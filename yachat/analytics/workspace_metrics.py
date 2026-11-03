"""A workspace's key metrics as SQL, and plain-English questions about them answered with text-to-SQL.

The tenant's audit records for a window are shaped into five tables in a locked, in-memory DuckDB (DatasetDB): only
that tenant's rows are loaded, queries are one SELECT with a timeout, and nothing outside the tables is reachable.
Each key metric is a named SQL query over those tables; the same definitions guide the model that writes SQL for a
question, so an answer uses the dashboard's own meaning of "completed" or "cost per conversation"."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pandas as pd

from yachat.analytics.duck import AnalyticsError, DatasetDB, QueryResult
from yachat.analytics.models import Column
from yachat.analytics.vanna_sql import VannaSQL
from yachat.logging import get_logger
from yachat.runtime.llm import LLMClient
from yachat.state.models import AuditEvent

log = get_logger("workspace_metrics")


@dataclass(frozen=True)
class KeyMetric:
    name: str
    label: str
    #: "count", "rate" (0-1), "ms", "usd", "minutes", "number"
    unit: str
    description: str
    sql: str


METRICS = [
    KeyMetric("conversations", "Conversations", "count", "Conversations with at least one turn.", "SELECT count(*) AS value FROM conversations"),
    KeyMetric(
        "completion_rate",
        "Completed",
        "rate",
        "Share of conversations that reached the end of their flow.",
        "SELECT avg(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS value FROM conversations",
    ),
    KeyMetric(
        "handoff_rate",
        "Handed to a person",
        "rate",
        "Share of conversations passed to a person.",
        "SELECT avg(CASE WHEN outcome = 'handed_off' THEN 1 ELSE 0 END) AS value FROM conversations",
    ),
    KeyMetric(
        "drop_rate",
        "Dropped",
        "rate",
        "Share of conversations that ended any other way: too many unclear answers, wandered off, declined, opted out.",
        "SELECT avg(CASE WHEN outcome NOT IN ('completed', 'handed_off', 'open') THEN 1 ELSE 0 END) AS value FROM conversations",
    ),
    KeyMetric(
        "turns_per_conversation",
        "Turns per conversation",
        "number",
        "Messages the user sent, on average, per conversation.",
        "SELECT avg(turns) AS value FROM conversations",
    ),
    KeyMetric(
        "p95_reply_ms",
        "Reply time, 95th percentile",
        "ms",
        "95% of replies took this long or less.",
        "SELECT quantile_disc(ms, 0.95) AS value FROM turns",
    ),
    KeyMetric(
        "error_turn_rate",
        "Turns with an error",
        "rate",
        "Share of turns where a model or tool call failed (the user still got a reply).",
        "SELECT avg(CASE WHEN errors > 0 THEN 1 ELSE 0 END) AS value FROM turns",
    ),
    KeyMetric(
        "cost_per_conversation",
        "Model cost per conversation",
        "usd",
        "USD spent on model calls, averaged over conversations. Calls with no known price count as zero.",
        "SELECT sum(cost_usd) / nullif(count(*), 0) AS value FROM conversations",
    ),
    KeyMetric(
        "total_cost",
        "Model cost",
        "usd",
        "USD spent on model calls in the window, including background memory writes.",
        "SELECT sum(cost_usd) AS value FROM model_calls",
    ),
    KeyMetric(
        "minutes_to_claim",
        "Time to claim a handoff (median)",
        "minutes",
        "Minutes from a handoff opening to a person claiming it.",
        "SELECT median(minutes_to_claim) AS value FROM handoffs",
    ),
]

TABLES: dict[str, tuple[str, dict[str, str]]] = {
    "conversations": (
        "One row per conversation (session) with at least one turn in the window.",
        {
            "outcome": "'completed', 'handed_off', 'open' (not ended yet) or another exit reason: max_reasks, disengaged, declined, opted_out, ...",
            "started_at": "time of the first turn",
            "cost_usd": "USD of the conversation's model calls (0 when no price is known)",
            "rejected_answers": "answers that failed validation and were asked again",
        },
    ),
    "turns": (
        "One row per user message the agent replied to.",
        {"route": "what the turn did: a step name, 'knowledge', 'opt_out', 'handoff', ...", "ms": "time to reply in milliseconds"},
    ),
    "model_calls": (
        "One row per model call, with tokens and cost.",
        {"purpose": "what the call was for: intent_detect, render, reply_verify, affirm, memory_extract, ...", "cost_usd": "NULL when the price is unknown"},
    ),
    "handoffs": (
        "One row per conversation passed to a person.",
        {"reason": "user_request, max_reasks, disengaged, tool_unavailable", "status": "open, claimed or resolved"},
    ),
    "decisions": (
        "Every decision the platform made (the audit log), one row each.",
        {
            "kind": "turn, answer, count, step, exit, handoff, guardrail, tool, llm",
            "decision": "e.g. 'accepted' / 'rejected' for answers, the exit reason for exits, 'blocked' for guardrails",
            "field": "the question an answer was for",
            "ref": "a handoff id, tool name, guardrail name or model",
        },
    ),
}

COLUMNS = {
    "conversations": ["session_id", "agent", "started_at", "day", "last_at", "turns", "outcome", "rejected_answers", "errors", "llm_calls", "cost_usd"],
    "turns": ["at", "day", "agent", "session_id", "turn_id", "route", "ms", "llm_calls", "tool_calls", "errors"],
    "model_calls": ["at", "day", "agent", "session_id", "model", "purpose", "input_tokens", "output_tokens", "cached_tokens", "cost_usd", "ms"],
    "handoffs": [
        "handoff_id",
        "agent",
        "session_id",
        "opened_at",
        "day",
        "reason",
        "status",
        "claimed_at",
        "resolved_at",
        "assignee",
        "minutes_to_claim",
        "minutes_to_resolve",
    ],
    "decisions": ["at", "day", "agent", "session_id", "turn_id", "kind", "decision", "reason", "step", "field", "ok", "ref", "actor"],
}

GUIDELINES = (
    "Write one DuckDB SELECT over these tables only. Rates are fractions between 0 and 1. A conversation's outcome is in "
    "conversations.outcome; never recount it from decisions. Use the column `day` (a DATE) for daily breakdowns. "
    "Round money to 4 decimals and times to whole milliseconds. Return at most a few hundred rows, ordered sensibly."
)

EXAMPLES = [{"question": f"What is the {m.label.lower()}?", "sql": m.sql} for m in METRICS] + [
    {
        "question": "Which agent has the highest handoff rate?",
        "sql": "SELECT agent, count(*) AS conversations, avg(CASE WHEN outcome = 'handed_off' THEN 1 ELSE 0 END) AS handoff_rate "
        "FROM conversations GROUP BY agent ORDER BY handoff_rate DESC",
    },
    {
        "question": "Completion rate per day",
        "sql": "SELECT day, count(*) AS conversations, avg(CASE WHEN outcome = 'completed' THEN 1 ELSE 0 END) AS completion_rate "
        "FROM conversations GROUP BY day ORDER BY day",
    },
]


def _day(at: datetime) -> Any:
    return at.date()


def tables(events: list[AuditEvent]) -> list[tuple[str, pd.DataFrame, str, dict[str, str]]]:
    """The five tables, from one tenant's audit records."""
    turns = [e for e in events if e.kind == "turn" and e.session_id]
    calls = [e for e in events if e.kind == "llm"]
    exits = {e.session_id: e.decision for e in sorted(events, key=lambda e: e.at) if e.kind == "exit" and e.session_id}
    rejected: dict[str, int] = {}
    for e in events:
        if e.kind == "answer" and e.decision == "rejected" and e.session_id:
            rejected[e.session_id] = rejected.get(e.session_id, 0) + 1
    cost: dict[str, float] = {}
    for e in calls:
        if e.session_id:
            cost[e.session_id] = cost.get(e.session_id, 0.0) + (e.cost_usd or 0.0)

    by_session: dict[str, list[AuditEvent]] = {}
    for e in turns:
        by_session.setdefault(str(e.session_id), []).append(e)
    conversations = []
    for sid, ts in by_session.items():
        ts.sort(key=lambda e: e.at)
        conversations.append(
            {
                "session_id": sid,
                "agent": ts[0].agent,
                "started_at": ts[0].at,
                "day": _day(ts[0].at),
                "last_at": ts[-1].at,
                "turns": len(ts),
                "outcome": exits.get(sid, "open"),
                "rejected_answers": rejected.get(sid, 0),
                "errors": sum(e.errors or 0 for e in ts),
                "llm_calls": sum(e.llm_calls or 0 for e in ts),
                "cost_usd": round(cost.get(sid, 0.0), 8),
            }
        )

    turn_rows = [
        {
            "at": e.at,
            "day": _day(e.at),
            "agent": e.agent,
            "session_id": e.session_id,
            "turn_id": e.turn_id,
            "route": e.decision,
            "ms": e.ms,
            "llm_calls": e.llm_calls or 0,
            "tool_calls": e.tool_calls or 0,
            "errors": e.errors or 0,
        }
        for e in turns
    ]
    call_rows = [
        {
            "at": e.at,
            "day": _day(e.at),
            "agent": e.agent,
            "session_id": e.session_id,
            "model": e.ref,
            "purpose": e.reason,
            "input_tokens": e.input_tokens or 0,
            "output_tokens": e.output_tokens or 0,
            "cached_tokens": e.cached_tokens or 0,
            "cost_usd": e.cost_usd,
            "ms": e.ms,
        }
        for e in calls
    ]

    handoffs: dict[str, dict[str, Any]] = {}
    for e in sorted(events, key=lambda e: e.at):
        if e.kind != "handoff" or not e.ref:
            continue
        if e.decision == "opened":
            handoffs[e.ref] = {
                "handoff_id": e.ref,
                "agent": e.agent,
                "session_id": e.session_id,
                "opened_at": e.at,
                "day": _day(e.at),
                "reason": e.reason,
                "status": "open",
                "claimed_at": None,
                "resolved_at": None,
                "assignee": None,
            }
        elif (h := handoffs.get(e.ref)) is not None and e.decision in ("claimed", "resolved"):
            if h[f"{e.decision}_at"] is None:
                h[f"{e.decision}_at"] = e.at
            h["status"] = e.decision
            if e.decision == "claimed":
                h["assignee"] = e.actor
    for h in handoffs.values():
        for k in ("claimed", "resolved"):
            at = h[f"{k}_at"]
            h[f"minutes_to_{'claim' if k == 'claimed' else 'resolve'}"] = round((at - h["opened_at"]).total_seconds() / 60, 1) if at else None

    decision_rows = [
        {
            "at": e.at,
            "day": _day(e.at),
            "agent": e.agent,
            "session_id": e.session_id,
            "turn_id": e.turn_id,
            "kind": e.kind,
            "decision": e.decision,
            "reason": e.reason,
            "step": e.step,
            "field": e.field,
            "ok": e.ok,
            "ref": e.ref,
            "actor": e.actor,
        }
        for e in events
    ]

    def frame(rows: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
        return pd.DataFrame(rows, columns=columns)

    rows = {"conversations": conversations, "turns": turn_rows, "model_calls": call_rows, "handoffs": list(handoffs.values()), "decisions": decision_rows}
    return [(name, _typed(frame(rows[name], COLUMNS[name])), *TABLES[name]) for name in TABLES]


_TYPES = {
    "at": "datetime64[us, UTC]",
    "started_at": "datetime64[us, UTC]",
    "last_at": "datetime64[us, UTC]",
    "opened_at": "datetime64[us, UTC]",
    "claimed_at": "datetime64[us, UTC]",
    "resolved_at": "datetime64[us, UTC]",
    "ms": "float64",
    "cost_usd": "float64",
    "minutes_to_claim": "float64",
    "minutes_to_resolve": "float64",
    "ok": "boolean",
    **dict.fromkeys(("turns", "rejected_answers", "errors", "llm_calls", "tool_calls", "input_tokens", "output_tokens", "cached_tokens"), "int64"),
}


def _typed(df: pd.DataFrame) -> pd.DataFrame:
    """Column types that hold for an empty table too, so a query on an empty window still runs."""
    df = df.astype({c: t for c, t in _TYPES.items() if c in df.columns})
    if "day" in df.columns:
        df["day"] = pd.to_datetime(df["day"]).dt.date if len(df) else pd.Series([], dtype="object")
    for col in df.columns:
        if df[col].dtype == object and col != "day":
            df[col] = df[col].astype("string")
    return df


def metrics_db(events: list[AuditEvent]) -> DatasetDB:
    return DatasetDB.from_frames(tables(events), memory_limit="256MB")


@dataclass
class MetricValue:
    metric: KeyMetric
    value: float | None


async def key_metrics(db: DatasetDB) -> list[MetricValue]:
    out = []
    for m in METRICS:
        r = await db.query(m.sql, row_cap=1, stats_cap=1, timeout_s=10.0)
        v = r.rows[0][0] if r.rows else None
        out.append(MetricValue(m, None if v is None else round(float(v), 6)))
    return out


@dataclass
class Answer:
    sql: str
    columns: list[Column]
    rows: list[list[Any]]
    truncated: bool
    attempts: int


#: a failed query is shown to the model with its error and rewritten this many times at most
REPAIRS = 2


async def ask(db: DatasetDB, llm: LLMClient, model: str, question: str) -> Answer:
    """Text to SQL over the workspace tables. AnalyticsError (`kind` is safe to show) when no attempt runs."""
    docs = [f"Key metric {m.name} ({m.label}): {m.description} SQL: {m.sql}" for m in METRICS] + [GUIDELINES]

    async def peek(sql: str) -> pd.DataFrame:  # Vanna may list a column's distinct values first
        return (await db.query(sql, row_cap=50, stats_cap=50, timeout_s=10.0)).frame

    vanna = VannaSQL(llm, model, db.ddl(), docs, EXAMPLES, peek, timeout_s=60.0)
    prompt, last = question, AnalyticsError("sql_invalid", "no attempt")
    for attempt in range(REPAIRS + 1):
        sql = (await vanna.generate_sql(prompt)).strip()
        try:
            r: QueryResult = await db.query(sql, row_cap=200, stats_cap=200, timeout_s=15.0)
            return Answer(sql, r.columns, r.rows, r.truncated, attempt + 1)
        except AnalyticsError as e:
            last = e
            if e.kind == "sql_timeout":
                break
            log.info("metrics sql repair", attempt=attempt + 1, kind=e.kind)
            prompt = (
                f"{question}\n\nA previous attempt produced this, which failed:\n{sql[:4000]}\nError: {str(e)[:1000]}\nWrite one corrected DuckDB SELECT query."
            )
    raise last

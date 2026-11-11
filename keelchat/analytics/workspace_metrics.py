"""A workspace's key metrics as SQL, and plain-English questions about them answered with text-to-SQL.

The tenant's audit records for a window (`runtime/audit.py:audit_db`) are shaped into five tables in SQL, then the
database is locked (DatasetDB): only that tenant's rows are loaded, queries are one SELECT with a timeout, and nothing
outside the tables is reachable.
Each key metric is a named SQL query over those tables; the same definitions guide the model that writes SQL for a
question, so an answer uses the dashboard's own meaning of "completed" or "cost per conversation"."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pandas as pd

from keelchat.analytics.duck import AnalyticsError, DatasetDB, QueryResult
from keelchat.analytics.models import Column
from keelchat.analytics.vanna_sql import VannaSQL
from keelchat.logging import get_logger
from keelchat.runtime.llm import LLMClient

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

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


#: each table from the window's `audit` rows; a conversation's outcome is its last exit, "open" without one
SQL = {
    "conversations": """
        WITH t AS (SELECT * FROM audit WHERE kind = 'turn' AND session_id <> ''),
        ex AS (SELECT session_id, arg_max(decision, "at") AS outcome FROM audit WHERE kind = 'exit' AND session_id <> '' GROUP BY 1),
        rj AS (SELECT session_id, count(*) AS n FROM audit WHERE kind = 'answer' AND decision = 'rejected' AND session_id <> '' GROUP BY 1),
        c AS (SELECT session_id, sum(coalesce(cost_usd, 0)) AS cost FROM audit WHERE kind = 'llm' AND session_id <> '' GROUP BY 1)
        SELECT t.session_id, arg_min(t.agent, t."at") AS agent, min(t."at") AS started_at, CAST(min(t."at") AS DATE) AS day,
            max(t."at") AS last_at, count(*) AS turns, coalesce(any_value(ex.outcome), 'open') AS outcome,
            coalesce(any_value(rj.n), 0) AS rejected_answers, coalesce(sum(t.errors), 0) AS errors, coalesce(sum(t.llm_calls), 0) AS llm_calls,
            round(coalesce(any_value(c.cost), 0), 8) AS cost_usd
        FROM t LEFT JOIN ex USING (session_id) LEFT JOIN rj USING (session_id) LEFT JOIN c USING (session_id) GROUP BY t.session_id
    """,
    "turns": """
        SELECT "at", CAST("at" AS DATE) AS day, agent, session_id, turn_id, decision AS route, ms, coalesce(llm_calls, 0) AS llm_calls,
            coalesce(tool_calls, 0) AS tool_calls, coalesce(errors, 0) AS errors
        FROM audit WHERE kind = 'turn' AND session_id <> ''
    """,
    "model_calls": """
        SELECT "at", CAST("at" AS DATE) AS day, agent, session_id, ref AS model, reason AS purpose, coalesce(input_tokens, 0) AS input_tokens,
            coalesce(output_tokens, 0) AS output_tokens, coalesce(cached_tokens, 0) AS cached_tokens, cost_usd, ms
        FROM audit WHERE kind = 'llm'
    """,
    "handoffs": """
        WITH h AS (SELECT * FROM audit WHERE kind = 'handoff' AND ref <> ''),
        o AS (SELECT ref, arg_max(agent, "at") AS agent, arg_max(session_id, "at") AS session_id, max("at") AS opened_at,
            arg_max(reason, "at") AS reason FROM h WHERE decision = 'opened' GROUP BY 1),
        d AS (SELECT o.ref, min(h."at") FILTER (h.decision = 'claimed') AS claimed_at, min(h."at") FILTER (h.decision = 'resolved') AS resolved_at,
            arg_max(h.decision, h."at") AS status, arg_max(h.actor, h."at") FILTER (h.decision = 'claimed') AS assignee
            FROM o JOIN h ON h.ref = o.ref AND h.decision IN ('claimed', 'resolved') AND h."at" >= o.opened_at GROUP BY 1)
        SELECT o.ref AS handoff_id, o.agent, o.session_id, o.opened_at, CAST(o.opened_at AS DATE) AS day, o.reason,
            coalesce(d.status, 'open') AS status, d.claimed_at, d.resolved_at, d.assignee,
            round(epoch(d.claimed_at - o.opened_at) / 60, 1) AS minutes_to_claim, round(epoch(d.resolved_at - o.opened_at) / 60, 1) AS minutes_to_resolve
        FROM o LEFT JOIN d USING (ref)
    """,
    "decisions": """
        SELECT "at", CAST("at" AS DATE) AS day, agent, session_id, turn_id, kind, decision, reason, step, field, ok, ref, actor FROM audit
    """,
}


def metrics_db(con: DuckDBPyConnection) -> DatasetDB:
    """The five tables from the window in `con` (`audit_db`), which is then locked; the raw `audit` table is dropped."""
    for name, sql in SQL.items():
        con.execute(f"CREATE TABLE {name} AS {sql}")
    con.execute("DROP TABLE audit")
    return DatasetDB.adopt(con, [(name, *TABLES[name]) for name in TABLES])


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

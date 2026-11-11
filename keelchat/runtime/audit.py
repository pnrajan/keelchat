"""The audit log: what the platform decided each turn, derived in code from the turn (state before and after, the
verdict, tool outcomes) plus what stages recorded themselves. Plus the summary the console and the API show."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from keelchat.policy.compiler import CompiledPolicy
from keelchat.runtime.context import TurnContext
from keelchat.runtime.redaction import redact_pii
from keelchat.runtime.usage import Usage
from keelchat.state.models import AuditEvent, AuditKind, SessionState, subject_of
from keelchat.state.store import CREATE_AUDIT, AuditStore

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

#: characters of user text an audit record keeps
SNIPPET_CHARS = 160
#: what a person wrote, kept longer than other snippets so insights read whole messages (still redacted, deletable)
MESSAGE_CHARS = 1000
#: days records are kept (AUDIT_RETENTION_DAYS overrides)
RETENTION_DAYS = 90


def snippet(ctx: TurnContext, text: str, limit: int = SNIPPET_CHARS) -> str:
    """A short, PII-redacted copy with the session's sensitive values removed; nothing from a flagged turn."""
    if ctx.input_flagged or not text:
        return ""
    for v in ctx.sensitive_values:
        if v:
            text = text.replace(v, "[sensitive]")
    text = redact_pii(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def events_for(ctx: TurnContext, before: SessionState, ms: float | None = None) -> list[AuditEvent]:
    """Every decision of one turn, in the order it was taken."""
    st, v, q = ctx.state, ctx.verdict, before.open_question

    def ev(kind: AuditKind, decision: str, **kw: object) -> AuditEvent:
        base: dict[str, object] = {"step": q.step if q else None, "field": q.field if q else None, "subject": subject_of(ctx.policy.tenant, st.user_id)}
        return AuditEvent.model_validate(
            {
                "tenant": ctx.policy.tenant,
                "agent": ctx.policy.agent,
                "policy_hash": ctx.policy.hash,
                "session_id": st.id,
                "turn_id": ctx.turn_id,
                "kind": kind,
                "decision": decision,
                **base,
                **kw,
            }
        )

    route = ctx.route or ("blocked" if ctx.short_circuit else "reply")
    out = [
        ev(
            "turn",
            route,
            reason=v.intent if v else "",
            text=snippet(ctx, ctx.inbound.text, MESSAGE_CHARS),
            reply=snippet(ctx, ctx.outbound.text if ctx.outbound else "", MESSAGE_CHARS),
            ms=round(ms, 1) if ms is not None else None,
            llm_calls=ctx.llm_calls,
            tool_calls=ctx.tool_calls,
            errors=len(ctx.errors),
        )
    ]
    for f in ctx.validated_answers:
        out.append(ev("answer", "accepted", field=f))
    if v is not None and v.intent == "edit" and st.data.get("edited_field"):
        out.append(ev("answer", "edited", field=str(st.data["edited_field"])))
    for f, n in st.reasks.items():
        if n > before.reasks.get(f, 0):
            out.append(ev("answer", "rejected", field=f, ref=str(n), reason=snippet(ctx, v.reason if v else "")))
    if st.digressions > before.digressions:
        out.append(ev("count", "digression", ref=str(st.digressions)))
    if st.nudges > before.nudges:
        out.append(ev("count", "nudge", ref=str(st.nudges)))
    for name, s in st.steps.items():
        was = before.steps.get(name)
        if s.status == "complete" and (was is None or was.status != "complete"):
            out.append(ev("step", "completed", step=name, field=None))
    for r in ctx.services.gateway.results:
        out.append(ev("tool", "ok" if r.ok else (r.error_kind or "failed"), ok=r.ok, ref=r.tool))
    out.extend(ctx.audit)
    if st.finished and not before.finished:
        out.append(ev("exit", st.exit_reason or ("completed" if st.completed else "exited"), ok=st.completed or None))
    return out


class AuditSummary(BaseModel):
    """Outcomes over a window: where conversations end, why they go to a person, which questions fail."""

    since: datetime | None
    until: datetime | None
    sessions: int
    turns: int
    #: how conversations ended (completed, handed_off, max_reasks, disengaged, ...)
    outcomes: dict[str, int]
    completion_rate: float
    handoff_rate: float
    #: why conversations went to a person
    handoff_reasons: dict[str, int]
    #: what people did on handoffs (claimed, resolved, ...)
    handoff_actions: dict[str, int]
    rejected_answers: dict[str, int]
    guardrails: dict[str, int]
    tool_failures: dict[str, int]


def _counts(db: DuckDBPyConnection, key: str, where: str) -> dict[str, int]:
    return {str(k): int(n) for k, n in db.execute(f"SELECT {key}, count(*) FROM audit WHERE {where} GROUP BY 1 ORDER BY 2 DESC, 1").fetchall()}


def _one(db: DuckDBPyConnection, sql: str) -> tuple[Any, ...]:
    row = db.execute(sql).fetchone()
    assert row is not None  # an aggregate without GROUP BY always returns a row
    return tuple(row)


def summarize(db: DuckDBPyConnection, since: datetime | None, until: datetime | None) -> AuditSummary:
    """Outcomes over the window loaded in `db` (`audit_db`), counted in SQL."""
    n, turns = _one(db, "SELECT count(DISTINCT session_id) FILTER (session_id <> ''), count(*) FROM audit WHERE kind = 'turn'")
    outcomes = _counts(db, "decision", "kind = 'exit'")
    return AuditSummary(
        since=since,
        until=until,
        sessions=n,
        turns=turns,
        outcomes=outcomes,
        completion_rate=round(outcomes.get("completed", 0) / n, 3) if n else 0.0,
        handoff_rate=round(outcomes.get("handed_off", 0) / n, 3) if n else 0.0,
        handoff_reasons=_counts(db, "reason", "kind = 'handoff' AND decision = 'opened'"),
        handoff_actions=_counts(db, "decision", "kind = 'handoff' AND actor <> 'runtime'"),
        rejected_answers=_counts(db, "coalesce(nullif(field, ''), '?')", "kind = 'answer' AND decision = 'rejected'"),
        guardrails=_counts(db, "coalesce(nullif(ref, ''), '?')", "kind = 'guardrail'"),
        tool_failures=_counts(db, "coalesce(nullif(ref, ''), '?')", "kind = 'tool' AND ok = false"),
    )


def retention_cutoff(days: int = RETENTION_DAYS) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


class DayPoint(BaseModel):
    day: str
    conversations: int
    completed: int
    handed_off: int
    #: ended any other way (max_reasks, disengaged, declined, ...)
    dropped: int
    turns: int
    p50_ms: float | None
    p95_ms: float | None
    error_turns: int


class AgentRow(BaseModel):
    agent: str
    conversations: int
    completion_rate: float
    handoff_rate: float
    drop_rate: float
    turns_per_conversation: float
    p95_ms: float | None
    llm_calls_per_turn: float
    top_rejected_field: str | None


class Dashboard(BaseModel):
    """A tenant's outcomes and service health over a window, from its audit log (turn records carry the trace)."""

    since: datetime
    until: datetime
    summary: AuditSummary
    days: list[DayPoint]
    agents: list[AgentRow]
    p50_ms: float | None
    p95_ms: float | None
    llm_calls_per_turn: float
    error_rate: float
    #: minutes from a handoff opening to a person claiming it, and to it being resolved
    claim_p50_min: float | None
    resolve_p50_min: float | None
    open_handoffs: int = 0


#: per conversation (a session with a turn): its agent, the day of its first turn and how it ended (its last exit)
_CONVERSATIONS = """
WITH ex AS (SELECT session_id, arg_max(decision, "at") AS outcome FROM audit WHERE kind = 'exit' AND session_id <> '' GROUP BY 1)
SELECT DISTINCT t.session_id, t.agent, min(t."at") OVER (PARTITION BY t.session_id) AS first_at, ex.outcome
FROM audit t LEFT JOIN ex USING (session_id) WHERE t.kind = 'turn' AND t.session_id <> ''
"""
_ENDED = """count(*) FILTER (outcome = 'completed') AS completed, count(*) FILTER (outcome = 'handed_off') AS handed_off,
count(*) FILTER (outcome NOT IN ('completed', 'handed_off')) AS dropped"""


def _p(v: float | None) -> float | None:
    return None if v is None else round(float(v), 1)


def dashboard(db: DuckDBPyConnection, since: datetime, until: datetime, open_handoffs: int = 0) -> Dashboard:
    """Outcomes and service health over the window loaded in `db` (`audit_db`), per day and per agent, in SQL."""
    convs = {str(d): r for d, *r in db.execute(f"SELECT CAST(first_at AS DATE), count(*), {_ENDED} FROM ({_CONVERSATIONS}) GROUP BY 1").fetchall()}
    turn_days = {
        str(d): r
        for d, *r in db.execute(
            'SELECT CAST("at" AS DATE), count(*), quantile_disc(ms, 0.5), quantile_disc(ms, 0.95), count(*) FILTER (errors > 0) '
            "FROM audit WHERE kind = 'turn' GROUP BY 1"
        ).fetchall()
    }
    days = []
    for i in range(max(1, (until.date() - since.date()).days + 1)):
        d = (since + timedelta(days=i)).date().isoformat()
        conversations, completed, handed_off, dropped = convs.get(d, (0, 0, 0, 0))
        turns, p50, p95, errors = turn_days.get(d, (0, None, None, 0))
        days.append(
            DayPoint(
                day=d,
                conversations=conversations,
                completed=completed,
                handed_off=handed_off,
                dropped=dropped,
                turns=turns,
                p50_ms=_p(p50),
                p95_ms=_p(p95),
                error_turns=errors,
            )
        )
    agents = []
    for agent, n, completed, handed_off, dropped, turns, p95, llm, top in db.execute(
        f"""
        WITH c AS (SELECT agent, count(*) AS n, {_ENDED} FROM ({_CONVERSATIONS}) GROUP BY 1),
        t AS (SELECT agent, count(*) AS turns, quantile_disc(ms, 0.95) AS p95, coalesce(sum(llm_calls), 0) AS llm FROM audit WHERE kind = 'turn' GROUP BY 1),
        r AS (SELECT agent, arg_max(field, n) AS top FROM (
            SELECT agent, field, count(*) AS n FROM audit WHERE kind = 'answer' AND decision = 'rejected' AND field <> '' GROUP BY 1, 2) GROUP BY 1)
        SELECT t.agent, coalesce(c.n, 0), coalesce(c.completed, 0), coalesce(c.handed_off, 0), coalesce(c.dropped, 0), t.turns, t.p95, t.llm, r.top
        FROM t LEFT JOIN c USING (agent) LEFT JOIN r USING (agent) ORDER BY t.agent
        """
    ).fetchall():
        per = n or 1
        agents.append(
            AgentRow(
                agent=agent,
                conversations=n,
                completion_rate=round(completed / per, 3),
                handoff_rate=round(handed_off / per, 3),
                drop_rate=round(dropped / per, 3),
                turns_per_conversation=round(turns / per, 1),
                p95_ms=_p(p95),
                llm_calls_per_turn=round(llm / (turns or 1), 2),
                top_rejected_field=top,
            )
        )
    turns, p50, p95, llm, errors = _one(
        db,
        "SELECT count(*), quantile_disc(ms, 0.5), quantile_disc(ms, 0.95), coalesce(sum(llm_calls), 0), count(*) FILTER (errors > 0) "
        "FROM audit WHERE kind = 'turn'",
    )

    def wait(decision: str) -> float | None:
        """Median minutes from each handoff opening to its first `decision`."""
        (v,) = _one(
            db,
            f"""WITH o AS (SELECT ref, min("at") AS opened FROM audit WHERE kind = 'handoff' AND decision = 'opened' AND ref <> '' GROUP BY 1),
            d AS (SELECT ref, min("at") AS done FROM audit WHERE kind = 'handoff' AND decision = '{decision}' GROUP BY 1)
            SELECT quantile_disc(epoch(done - opened) / 60, 0.5) FROM o JOIN d USING (ref)""",
        )
        return _p(v)

    return Dashboard(
        since=since,
        until=until,
        summary=summarize(db, since, until),
        days=days,
        agents=agents,
        p50_ms=_p(p50),
        p95_ms=_p(p95),
        llm_calls_per_turn=round(llm / (turns or 1), 2),
        error_rate=round(errors / (turns or 1), 3),
        claim_p50_min=wait("claimed"),
        resolve_p50_min=wait("resolved"),
        open_handoffs=open_handoffs,
    )


def llm_events(policy: CompiledPolicy, session_id: str, turn_id: str, calls: list[Usage]) -> list[AuditEvent]:
    """One record per model call: model (ref), purpose (reason), tokens, time and cost."""
    return [
        AuditEvent(
            tenant=policy.tenant,
            agent=policy.agent,
            policy_hash=policy.hash,
            session_id=session_id,
            turn_id=turn_id,
            kind="llm",
            decision="call",
            reason=c.purpose,
            ref=c.model,
            ms=c.ms,
            input_tokens=c.input_tokens,
            output_tokens=c.output_tokens,
            cached_tokens=c.cached_tokens,
            cost_usd=c.cost_usd,
            saved_tokens=c.saved_tokens or None,
        )
        for c in calls
    ]


class UsageLine(BaseModel):
    key: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    #: input tokens a Headroom proxy removed before the calls (not billed)
    saved_tokens: int = 0
    #: None when no call in the group had a known price
    cost_usd: float | None
    #: calls with no known price (their tokens are counted, their cost is not)
    unpriced_calls: int
    p50_ms: float | None


class UsageReport(BaseModel):
    """A tenant's model usage and cost over a window."""

    since: datetime
    until: datetime
    total: UsageLine
    by_model: list[UsageLine]
    by_purpose: list[UsageLine]
    by_agent: list[UsageLine]
    by_day: list[UsageLine]
    conversations: int
    cost_per_conversation: float | None


#: DuckDB memory for one dashboard query (it spills to a temporary file beyond this)
AUDIT_DB_MEMORY = "1GB"


async def audit_db(
    store: AuditStore, tenant: str, since: datetime, until: datetime | None = None, *, agent: str | None = None, kinds: list[AuditKind] | None = None
) -> DuckDBPyConnection:
    """A tenant's records for a window as the table `audit` in a private in-memory DuckDB, for the dashboards. The
    store loads it: Postgres by COPY, the Iceberg archive by DuckDB reading the files on S3. The caller closes it."""
    import duckdb

    config: dict[str, Any] = {"TimeZone": "UTC", "memory_limit": AUDIT_DB_MEMORY}
    if ext := os.getenv("KEELCHAT_DUCKDB_EXTENSIONS"):  # where the image installed iceberg / httpfs / aws (no download)
        config["extension_directory"] = ext
    db = duckdb.connect(config=config)
    try:
        db.execute(CREATE_AUDIT)
        await store.copy_to(db, tenant, since=since, until=until, agent=agent, kinds=kinds)
    except BaseException:
        db.close()
        raise
    return db


_USAGE = """
SELECT {key} AS key, count(*) AS calls, coalesce(sum(input_tokens), 0) AS input_tokens, coalesce(sum(output_tokens), 0) AS output_tokens,
       coalesce(sum(cached_tokens), 0) AS cached_tokens, coalesce(sum(saved_tokens), 0) AS saved_tokens, round(sum(cost_usd), 6) AS cost_usd,
       count(*) - count(cost_usd) AS unpriced_calls, round(quantile_disc(ms, 0.5), 1) AS p50_ms
FROM audit WHERE kind = 'llm' {group}
"""


def _lines(db: DuckDBPyConnection, key: str, grouped: bool = True) -> list[UsageLine]:
    cur = db.execute(_USAGE.format(key=key, group="GROUP BY 1 ORDER BY 1" if grouped else ""))
    names = [d[0] for d in cur.description or []]
    lines = [UsageLine(**dict(zip(names, row, strict=True))) for row in cur.fetchall()]
    return sorted(lines, key=lambda u: (-(u.cost_usd or 0), -u.input_tokens)) if grouped else lines


def usage_report(db: DuckDBPyConnection, since: datetime, until: datetime) -> UsageReport:
    """Totals in DuckDB over the window's `llm` and `turn` records (see `audit_db`): no row reaches Python."""
    (total,) = _lines(db, "'total'", grouped=False)
    found = {u.key: u for u in _lines(db, "strftime(CAST(\"at\" AS DATE), '%Y-%m-%d')")}
    n_days = max(1, (until.date() - since.date()).days + 1)
    days = [(since + timedelta(days=i)).date().isoformat() for i in range(n_days)]
    empty: dict[str, Any] = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost_usd": None, "unpriced_calls": 0, "p50_ms": None}
    row = db.execute("SELECT count(DISTINCT session_id) FROM audit WHERE kind = 'turn'").fetchone()
    sessions = int(row[0]) if row else 0
    return UsageReport(
        since=since,
        until=until,
        total=total,
        by_model=_lines(db, "coalesce(nullif(ref, ''), '?')"),
        by_purpose=_lines(db, "coalesce(nullif(reason, ''), '?')"),
        by_agent=_lines(db, "agent"),
        by_day=[found.get(d) or UsageLine(key=d, **empty) for d in days],
        conversations=sessions,
        cost_per_conversation=round(total.cost_usd / sessions, 6) if total.cost_usd is not None and sessions else None,
    )

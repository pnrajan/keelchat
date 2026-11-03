"""The audit log: what the platform decided each turn, derived in code from the turn (state before and after, the
verdict, tool outcomes) plus what stages recorded themselves. Plus the summary the console and the API show."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel

from yachat.policy.compiler import CompiledPolicy
from yachat.runtime.context import TurnContext
from yachat.runtime.redaction import redact_pii
from yachat.runtime.usage import Usage
from yachat.state.models import AuditEvent, AuditKind, SessionState, subject_of

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
    #: true when the window held more records than were read
    truncated: bool = False


def summarize(events: list[AuditEvent], since: datetime | None, until: datetime | None, truncated: bool = False) -> AuditSummary:
    sessions = {e.session_id for e in events if e.kind == "turn" and e.session_id}
    outcomes = Counter(e.decision for e in events if e.kind == "exit")
    n = len(sessions)
    return AuditSummary(
        since=since,
        until=until,
        sessions=n,
        turns=sum(1 for e in events if e.kind == "turn"),
        outcomes=dict(outcomes),
        completion_rate=round(outcomes.get("completed", 0) / n, 3) if n else 0.0,
        handoff_rate=round(outcomes.get("handed_off", 0) / n, 3) if n else 0.0,
        handoff_reasons=dict(Counter(e.reason for e in events if e.kind == "handoff" and e.decision == "opened")),
        handoff_actions=dict(Counter(e.decision for e in events if e.kind == "handoff" and e.actor != "runtime")),
        rejected_answers=dict(Counter(e.field or "?" for e in events if e.kind == "answer" and e.decision == "rejected")),
        guardrails=dict(Counter(e.ref or "?" for e in events if e.kind == "guardrail")),
        tool_failures=dict(Counter(e.ref or "?" for e in events if e.kind == "tool" and e.ok is False)),
        truncated=truncated,
    )


def retention_cutoff(days: int = RETENTION_DAYS) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return round(s[max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))], 1)


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


def dashboard(events: list[AuditEvent], since: datetime, until: datetime, open_handoffs: int = 0, truncated: bool = False) -> Dashboard:
    turns = [e for e in events if e.kind == "turn"]
    exits = {e.session_id: e.decision for e in events if e.kind == "exit" and e.session_id}
    first_day: dict[str, str] = {}
    for e in sorted(turns, key=lambda e: e.at):
        if e.session_id and e.session_id not in first_day:
            first_day[e.session_id] = e.at.date().isoformat()
    n_days = max(1, (until.date() - since.date()).days + 1)
    days = []
    for i in range(n_days):
        d = (since + timedelta(days=i)).date().isoformat()
        sids = [s for s, fd in first_day.items() if fd == d]
        day_turns = [e for e in turns if e.at.date().isoformat() == d]
        ms = [e.ms for e in day_turns if e.ms is not None]
        outs = [exits.get(s) for s in sids]
        days.append(
            DayPoint(
                day=d,
                conversations=len(sids),
                completed=outs.count("completed"),
                handed_off=outs.count("handed_off"),
                dropped=sum(1 for o in outs if o not in (None, "completed", "handed_off")),
                turns=len(day_turns),
                p50_ms=_pct(ms, 50),
                p95_ms=_pct(ms, 95),
                error_turns=sum(1 for e in day_turns if e.errors),
            )
        )
    agents = []
    for a in sorted({e.agent for e in turns}):
        agent_sids = {e.session_id for e in turns if e.agent == a and e.session_id}
        a_turns = [e for e in turns if e.agent == a]
        outs = [exits.get(s) for s in agent_sids]
        n = len(agent_sids) or 1
        rejected = Counter(e.field for e in events if e.agent == a and e.kind == "answer" and e.decision == "rejected" and e.field)
        agents.append(
            AgentRow(
                agent=a,
                conversations=len(agent_sids),
                completion_rate=round(outs.count("completed") / n, 3),
                handoff_rate=round(outs.count("handed_off") / n, 3),
                drop_rate=round(sum(1 for o in outs if o not in (None, "completed", "handed_off")) / n, 3),
                turns_per_conversation=round(len(a_turns) / n, 1),
                p95_ms=_pct([e.ms for e in a_turns if e.ms is not None], 95),
                llm_calls_per_turn=round(sum(e.llm_calls or 0 for e in a_turns) / (len(a_turns) or 1), 2),
                top_rejected_field=rejected.most_common(1)[0][0] if rejected else None,
            )
        )
    opened = {e.ref: e.at for e in events if e.kind == "handoff" and e.decision == "opened" and e.ref}

    def waits(decision: str) -> list[float]:
        first: dict[str, datetime] = {}
        for e in sorted(events, key=lambda e: e.at):
            if e.kind == "handoff" and e.decision == decision and e.ref in opened and e.ref not in first:
                first[e.ref] = e.at
        return [(t - opened[r]).total_seconds() / 60 for r, t in first.items()]

    ms_all = [e.ms for e in turns if e.ms is not None]
    return Dashboard(
        since=since,
        until=until,
        summary=summarize(events, since, until, truncated),
        days=days,
        agents=agents,
        p50_ms=_pct(ms_all, 50),
        p95_ms=_pct(ms_all, 95),
        llm_calls_per_turn=round(sum(e.llm_calls or 0 for e in turns) / (len(turns) or 1), 2),
        error_rate=round(sum(1 for e in turns if e.errors) / (len(turns) or 1), 3),
        claim_p50_min=_pct(waits("claimed"), 50),
        resolve_p50_min=_pct(waits("resolved"), 50),
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
        )
        for c in calls
    ]


class UsageLine(BaseModel):
    key: str
    calls: int
    input_tokens: int
    output_tokens: int
    cached_tokens: int
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
    truncated: bool = False


def _line(key: str, xs: list[AuditEvent]) -> UsageLine:
    priced = [e.cost_usd for e in xs if e.cost_usd is not None]
    return UsageLine(
        key=key,
        calls=len(xs),
        input_tokens=sum(e.input_tokens or 0 for e in xs),
        output_tokens=sum(e.output_tokens or 0 for e in xs),
        cached_tokens=sum(e.cached_tokens or 0 for e in xs),
        cost_usd=round(sum(priced), 6) if priced else None,
        unpriced_calls=len(xs) - len(priced),
        p50_ms=_pct([e.ms for e in xs if e.ms is not None], 50),
    )


def usage_report(events: list[AuditEvent], since: datetime, until: datetime, truncated: bool = False) -> UsageReport:
    calls = [e for e in events if e.kind == "llm"]

    def group(key: Callable[[AuditEvent], str]) -> list[UsageLine]:
        buckets: dict[str, list[AuditEvent]] = {}
        for e in calls:
            buckets.setdefault(key(e), []).append(e)
        return sorted((_line(k, v) for k, v in buckets.items()), key=lambda u: (-(u.cost_usd or 0), -u.input_tokens))

    total = _line("total", calls)
    n_days = max(1, (until.date() - since.date()).days + 1)
    by_day = [_line(d, [e for e in calls if e.at.date().isoformat() == d]) for d in ((since + timedelta(days=i)).date().isoformat() for i in range(n_days))]
    sessions = {e.session_id for e in events if e.kind == "turn" and e.session_id}
    return UsageReport(
        since=since,
        until=until,
        total=total,
        by_model=group(lambda e: e.ref or "?"),
        by_purpose=group(lambda e: e.reason or "?"),
        by_agent=group(lambda e: e.agent),
        by_day=by_day,
        conversations=len(sessions),
        cost_per_conversation=round(total.cost_usd / len(sessions), 6) if total.cost_usd is not None and sessions else None,
        truncated=truncated,
    )

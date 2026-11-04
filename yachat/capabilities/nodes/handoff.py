"""Handoff: the conversation goes to a person, and comes back when they are done.

When a trigger fires, a ticket opens in the handoff inbox and (with `handoff.notify`) the receiver gets it with a
`callback_url`. Whoever works the ticket (a person in the inbox, or your helpdesk through the callback) reports back
once: `resolved` resumes the conversation (the default) or ends it. Until then the user's messages join the ticket.
With `handoff.timeout`, the assistant takes the conversation back by itself after that long."""

from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime
from typing import Any

from yachat.logging import get_logger
from yachat.registry.core import REGISTRY, EmptyConfig, Node, NodeResult, node
from yachat.runtime.context import TurnContext
from yachat.state.models import Handoff as HandoffRecord
from yachat.state.models import HandoffLine, SessionState, now

log = get_logger("handoff")

#: turns of conversation the person receives with the ticket
TRANSCRIPT_TURNS = 20
HANDED_OFF = "handed_off"
#: a person resolved the handoff and ended the conversation
RESOLVED = "resolved_by_person"


def callback_token() -> tuple[str, str]:
    """(token for the receiver, hash kept on the ticket)."""
    token = secrets.token_urlsafe(24)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def callback_url(tenant: str, ticket_id: str, token: str) -> str:
    """Absolute when YACHAT_PUBLIC_URL is set, else the path on this server."""
    base = (os.getenv("YACHAT_PUBLIC_URL") or "").rstrip("/")
    return f"{base}/v1/public/handoffs/{tenant}/{ticket_id}/callback?token={token}"


def take_back(st: SessionState, flow_fields: list[str], *, resume: bool = True, fields: dict[str, str] | None = None) -> bool:
    """End the session's open handoff: hand the conversation back to the assistant (`resume`) or end it. The counts that
    led to the handoff start over, so it does not hand straight off again; `fields` are answers the person collected.
    False when the session has no open handoff (already taken back, or another ticket)."""
    opened = st.data.get("handoff")
    if not opened or st.exit_reason != HANDED_OFF:
        return False
    unknown = sorted(set(fields or {}) - set(flow_fields))
    if unknown:
        raise ValueError(f"fields not in this agent's flow: {unknown}")
    st.prefilled.update(fields or {})  # offered back to the user like an answer given early, never trusted as validated
    st.reasks, st.digressions, st.nudges, st.tool_failures = {}, 0, 0, {}
    del st.data["handoff"]
    if resume:
        st.exited, st.exit_reason = False, None
        st.data["resumed"] = True
        st.data["back_at"] = now().isoformat()
    else:
        st.exit_reason = RESOLVED
    return True


def timed_out(st: SessionState, timeout_s: float | None, at: datetime | None = None) -> bool:
    opened = st.data.get("handoff") or {}
    if not timeout_s or st.exit_reason != HANDED_OFF or "opened_at" not in opened:
        return False
    return ((at or now()) - datetime.fromisoformat(opened["opened_at"])).total_seconds() >= timeout_s


def may_hand_off(ctx: TurnContext) -> bool:
    """The conversation goes to a person only with the user's consent, when the policy asks for it."""
    asks_consent = any(s.node == "greet_consent" for s in ctx.policy.steps)
    return ctx.state.consent is True or (ctx.state.consent is None and not asks_consent)


def _transcript(ctx: TurnContext) -> list[HandoffLine]:
    """Recent turns and this message; flagged turns are never passed on."""
    turns = [HandoffLine(role=t.role, text=t.text, at=t.created_at) for t in ctx.state.turns if not t.flagged][-TRANSCRIPT_TURNS:]
    if not ctx.input_flagged:
        turns.append(HandoffLine(role="user", text=ctx.text))
    return turns


def _lines(lines: list[HandoffLine]) -> list[dict[str, str]]:
    return [{"role": x.role, "text": x.text} for x in lines]


@node("handoff")
class Handoff(Node[EmptyConfig]):
    """Passes the conversation to a person: a record in the platform's handoff inbox, and (with `handoff.notify`) a
    call to a helpdesk tool. Later messages are added to the same handoff. Runs from the policy's `handoff` triggers,
    not as a flow step."""

    async def act(self, ctx: TurnContext, config: EmptyConfig) -> NodeResult:
        h, st, inbox = ctx.policy.handoff, ctx.state, ctx.services.handoffs
        q = st.open_question
        then_ask = f" {q.ask}" if q else ""
        if h is None or inbox is None:
            return NodeResult("I can't connect you with a person here." + then_ask, goal="Say you can't connect them with a person in this chat.")
        fields = {k: str(v) for k, v in st.fields.items() if v is not None}
        opened = st.data.get("handoff")
        if opened:  # already with a person: this message goes to the same handoff
            new = _transcript(ctx)[-1:] if not ctx.input_flagged else []
            await inbox.add_messages(st.tenant, opened["ticket_id"], new)
            ctx.decide("handoff", "follow_up", ref=opened["ticket_id"])
            if h.notify:
                await self._notify(ctx, h.notify, opened["ticket_id"], "follow_up", fields, new)
            return NodeResult(
                "I've added this to your conversation with our team.", goal="Say a person from the team has their conversation and will reply; nothing else."
            )
        reason = ctx.handoff_reason or "user_request"
        if not may_hand_off(ctx):
            log.info("handoff declined", reason="no consent")
            ctx.decide("handoff", "declined", reason="no_consent")
            return NodeResult(
                "I can connect you with a person once you agree to let us use your answers." + then_ask,
                goal="Say you can connect them with a person once they agree to let you use their answers, then ask the question.",
            )
        lines = _transcript(ctx)
        token, hashed = callback_token()
        record = await inbox.create(
            HandoffRecord(
                tenant=st.tenant,
                agent=ctx.policy.agent,
                session_id=st.id,
                user_id=st.user_id,
                channel=st.channel,
                policy_hash=ctx.policy.hash,
                reason=reason,
                fields=fields,
                transcript=lines,
                callback_hash=hashed,
            )
        )
        ctx.decide("handoff", "opened", reason=reason, ref=record.id)
        if h.notify:
            external = await self._notify(ctx, h.notify, record.id, reason, fields, lines, callback_url(st.tenant, record.id, token))
            if external:
                await inbox.update(st.tenant, record.id, external_id=external)
        st.data["handoff"] = {"ticket_id": record.id, "reason": reason, "opened_at": now().isoformat()}
        st.exited, st.exit_reason, st.open_question = True, HANDED_OFF, None
        ctx.exit_reason = HANDED_OFF
        log.info("handed off", reason=reason, handoff=record.id)
        return NodeResult(
            "I've passed this to a person on our team; they'll pick it up from here.",
            goal="Say a person on the team now has the conversation and will pick it up from here; ask nothing.",
            items=[{"type": "handoff", "ticket_id": record.id, "reason": reason}],
            exit_reason=HANDED_OFF,
        )

    @staticmethod
    async def _notify(
        ctx: TurnContext, tool: str, ticket_id: str, reason: str, fields: dict[str, str], lines: list[HandoffLine], callback: str | None = None
    ) -> str | None:
        """Tell the helpdesk through the policy's notify tool, with the callback it reports back on (when the tool takes
        one). The handoff stands either way; a failure is audited."""
        args: dict[str, Any] = {
            "session_id": ctx.state.id,
            "agent": ctx.policy.agent,
            "fields": fields,
            "reason": reason,
            "ticket_id": ticket_id,
            "transcript": _lines(lines),
        }
        if callback and "callback_url" in REGISTRY.tool(tool, ctx.policy.tenant).input_model.model_fields:
            args["callback_url"] = callback
        res = await ctx.services.gateway.call(tool, args)
        ctx.tool_calls += 1
        if not res.ok:
            log.error("handoff notify failed", tool=tool, error_kind=res.error_kind)
            ctx.decide("handoff", "notify_failed", reason=res.error_kind or "failed", ref=ticket_id, ok=False)
            return None
        external = (res.data or {}).get("ticket_id")
        return str(external) if external and str(external) != ticket_id else None

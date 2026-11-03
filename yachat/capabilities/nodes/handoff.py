from __future__ import annotations

from yachat.logging import get_logger
from yachat.registry.core import EmptyConfig, Node, NodeResult, node
from yachat.runtime.context import TurnContext
from yachat.state.models import Handoff as HandoffRecord
from yachat.state.models import HandoffLine

log = get_logger("handoff")

#: turns of conversation the person receives with the ticket
TRANSCRIPT_TURNS = 20
HANDED_OFF = "handed_off"


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
            )
        )
        ctx.decide("handoff", "opened", reason=reason, ref=record.id)
        if h.notify:
            external = await self._notify(ctx, h.notify, record.id, reason, fields, lines)
            if external:
                await inbox.update(st.tenant, record.id, external_id=external)
        st.data["handoff"] = {"ticket_id": record.id, "reason": reason}
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
    async def _notify(ctx: TurnContext, tool: str, ticket_id: str, reason: str, fields: dict[str, str], lines: list[HandoffLine]) -> str | None:
        """Tell the helpdesk through the policy's notify tool. The handoff stands either way; a failure is audited."""
        args = {"session_id": ctx.state.id, "agent": ctx.policy.agent, "fields": fields, "reason": reason, "ticket_id": ticket_id, "transcript": _lines(lines)}
        res = await ctx.services.gateway.call(tool, args)
        ctx.tool_calls += 1
        if not res.ok:
            log.error("handoff notify failed", tool=tool, error_kind=res.error_kind)
            ctx.decide("handoff", "notify_failed", reason=res.error_kind or "failed", ref=ticket_id, ok=False)
            return None
        external = (res.data or {}).get("ticket_id")
        return str(external) if external and str(external) != ticket_id else None

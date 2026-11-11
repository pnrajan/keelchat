from __future__ import annotations

import re

from keelchat.capabilities.nodes.handoff import HANDED_OFF, take_back, timed_out
from keelchat.logging import get_logger
from keelchat.registry.core import PreProcessor, preprocessor
from keelchat.runtime.context import TurnContext

log = get_logger("handoff_request")

#: plain asks for a person; subtler ones are left to the Intent Detector (`wants_human`)
PATTERNS = (
    r"\b(talk|speak|chat)\s+(to|with)\s+(a|an|some)?\s*(real|live|actual)?\s*(human|person|agent|representative|someone)\b",
    r"\b(human|live|real)\s+(agent|person|representative|being)\b",
    r"^\s*(human|representative|operator)\s*(please)?\s*[.!?]*\s*$",
    r"\bhablar con (una persona|un humano|un agente|alguien)\b",
)


def asks_for_person(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in PATTERNS)


@preprocessor("handoff_request", order=16)
class HandoffRequest(PreProcessor):
    """Routes a message to the handoff step: a plain ask for a person (when the policy hands off on user_request), or
    any message after the conversation was handed off."""

    async def run(self, ctx: TurnContext) -> TurnContext:
        h, st = ctx.policy.handoff, ctx.state
        if h is None:
            return ctx
        timeout = h.timeout.total_seconds() if h.timeout else None
        if timed_out(st, timeout):  # nobody reported back in time: the assistant takes the conversation back
            ticket = st.data["handoff"]["ticket_id"]
            take_back(st, ctx.policy.fields)
            ctx.decide("handoff", "timed_out", ref=ticket)
            log.info("handoff timed out; resuming", handoff=ticket)
        if st.exit_reason == HANDED_OFF:
            ctx.handoff_reason = "follow_up"
        elif not st.finished and "user_request" in h.when and asks_for_person(ctx.text):
            ctx.handoff_reason = "user_request"
            log.info("handoff requested", reason="explicit ask")
        return ctx

"""A flow step: looks up open slots with the package's own tool, offers them, and takes the client's pick.

The step validates the pick in code (a number or a listed time); the model only phrases the replies from `goal`.
It reaches the tool only through the Tool Gateway (`ctx.services.gateway`), like every built-in step."""

from __future__ import annotations

from typing import Any

from keelchat.capabilities.nodes.common import answers_for
from keelchat.registry.core import CapabilityConfig, Node, NodeResult, node
from keelchat.runtime.context import TurnContext

ASK = ("slot", "Which of these times suits you? Reply with its number.", "the number or time of one listed slot")


class PickSlotConfig(CapabilityConfig):
    #: the earlier answers holding the service and the preferred day
    service_field: str = "service"
    day_field: str = "preferred_day"


@node("pick_slot", tools=["slot_search"], produces=["slot"])
class PickSlot(Node[PickSlotConfig]):
    """Offers the open appointment slots for the chosen service and day, and books the one the client picks."""

    config_model = PickSlotConfig

    async def act(self, ctx: TurnContext, config: PickSlotConfig) -> NodeResult:
        slots: list[str] = ctx.state.data.get("slots", [])
        picked = answers_for(ctx, "pick_slot").get("slot")
        if picked and slots:
            choice = _match(picked, slots)
            if choice:
                return NodeResult(
                    f"Booked: {choice}.", goal=f"Confirm the booking for {choice} warmly, in one sentence.", outputs={"slot": choice}, complete=True
                )
            return NodeResult("Please reply with the number of a listed time.", goal="Their reply didn't match a listed time: ask again.", ask=ASK)
        args = {"service": str(ctx.state.fields.get(config.service_field, "")), "day": str(ctx.state.fields.get(config.day_field, ""))}
        res = await ctx.services.gateway.call("slot_search", args)
        ctx.tool_calls += 1
        found: list[str] = list(res.data.get("slots", [])) if res.ok and isinstance(res.data, dict) else []
        if not found:
            # nothing to offer: a dead end the policy can hand to a person (handoff.when lists it)
            return NodeResult(
                "There are no open slots for that day. I'll pass you to the salon team.",
                goal="Say nothing is open for that service and day, and that the salon team will follow up.",
                exit_reason="tool_unavailable" if not res.ok else "max_reasks",
            )
        ctx.state.data["slots"] = found
        items: list[dict[str, Any]] = [{"title": s} for s in found]
        return NodeResult(ASK[1], goal="List the open times and ask which one suits them, by number.", items=items, ask=ASK)


def _match(reply: str, slots: list[str]) -> str | None:
    digits = "".join(ch for ch in reply if ch.isdigit())
    if digits and len(digits) <= 2 and 0 < int(digits) <= len(slots):
        return slots[int(digits) - 1]
    low = reply.lower()
    return next((s for s in slots if s.lower() in low or s.split(" ", 1)[-1] in low), None)

from __future__ import annotations

from yachat.logging import get_logger
from yachat.registry.core import PreProcessor, preprocessor
from yachat.runtime.context import TurnContext
from yachat.state.flow import StepGraph

log = get_logger("receiver")


@preprocessor("receiver", order=10)
class Receiver(PreProcessor):
    """Loads the step graph and the user's memory; ends finished sessions. Lock and dedupe wrap the pipeline in Runtime."""

    async def run(self, ctx: TurnContext) -> TurnContext:
        StepGraph(ctx.policy).init(ctx.state)
        if ctx.policy.memory and ctx.services.memory is not None:
            threshold = ctx.policy.defaults.stale_memory_threshold
            ctx.memory = [m for m in await ctx.services.memory.list(ctx.state.user_id, ctx.policy.agent, ctx.policy.tenant) if m.weight() >= threshold]
            ctx.services.gateway.sensitive_values = ctx.sensitive_values
            if ctx.services.actions is not None:
                ctx.services.actions.sensitive_values = ctx.sensitive_values
        if ctx.state.exited and ctx.state.exit_reason not in ("completed", "handed_off"):  # a handed-off one goes to the ticket
            msg = "This conversation has ended."
            if ctx.state.resume_token:
                msg += " Start a new session with your resume token to continue."
            ctx.short_circuit_with(msg, note="session finished")
        log.info("session loaded", turns=len(ctx.state.turns), memory_items=len(ctx.memory), finished=ctx.state.finished)
        return ctx

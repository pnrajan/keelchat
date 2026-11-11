from __future__ import annotations

from keelchat.logging import get_logger
from keelchat.registry.core import EmptyConfig, Node, NodeResult, node
from keelchat.runtime.context import TurnContext
from keelchat.state.flow import StepGraph

log = get_logger("exit")


@node("exit")
class Exit(Node[EmptyConfig]):
    """Sends the exit message, saves progress, issues a resume token, logs the reason."""

    async def act(self, ctx: TurnContext, config: EmptyConfig) -> NodeResult:
        reason = ctx.exit_reason or "completed"
        token = StepGraph(ctx.policy).exit(ctx.state, reason)
        ctx.exit_reason = reason
        log.info("exit", reason=reason, fields=sorted(ctx.state.fields), has_resume_token=bool(token))
        # a finished conversation has nothing left to come back for; any other exit keeps the resume invitation
        text = "Thank you!" if reason == "completed" else ctx.policy.defaults.exit_message
        goal = "Thank them warmly in a few words; nothing else is needed." if reason == "completed" else "Close politely and invite them back to finish later."
        return NodeResult(text, goal=goal, items=[{"type": "resume_token", "token": token}], exit_reason=reason)

from __future__ import annotations

from keelchat.channels import CHANNELS
from keelchat.runtime.context import TurnContext


class ChannelStage:
    name = "channel"

    async def run(self, ctx: TurnContext) -> TurnContext:
        if ctx.outbound is not None:
            ctx.rendered = CHANNELS[ctx.inbound.channel].render(ctx.outbound)
        return ctx

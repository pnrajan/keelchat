"""Output guardrails on the draft, with step context so requested PII is allowed."""

from __future__ import annotations

from keelchat.logging import get_logger
from keelchat.runtime.context import TurnContext
from keelchat.runtime.understand.guardrails import run_guardrails

log = get_logger("output_guardrails")

BLOCKED = {"en": "I can't share that. Let's continue.", "es": "No puedo compartir eso. Sigamos."}


class OutputGuardrails:
    name = "output_guardrails"

    async def run(self, ctx: TurnContext) -> TurnContext:
        if ctx.node_result is None:
            return ctx
        text = ctx.node_result.text
        # One at a time, each on the text the previous one left: redactions compose instead of the last flagged check
        # replacing the others' work on the original, and a block ends the chain (nothing later can restore the text).
        for name in ctx.policy.defaults.output_guardrails:
            ((_, r),) = await run_guardrails([name], text, ctx, "output")
            if not r.flagged:
                continue
            log.warning("output flagged", guardrail=name, reason=r.reason, step=ctx.guard.step, field=ctx.guard.field)
            ctx.errors.append(f"output guardrail {name}: {r.reason}")
            ctx.decide("guardrail", "redacted_output" if r.redacted is not None else "blocked_output", reason=r.reason, ref=name)
            if r.redacted is None:
                text = BLOCKED.get(ctx.language, BLOCKED["en"])
                break
            text = r.redacted
        ctx.node_result.text = text
        return ctx

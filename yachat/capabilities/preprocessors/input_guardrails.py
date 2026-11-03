from __future__ import annotations

from yachat.logging import get_logger
from yachat.registry.core import PreProcessor, preprocessor
from yachat.runtime.context import TurnContext
from yachat.runtime.understand.guardrails import run_guardrails

log = get_logger("input_guardrails")

REFUSAL = {
    "en": "I can't help with that request. Let's continue with your application.",
    "es": "No puedo ayudar con esa solicitud. Sigamos con tu solicitud de empleo.",
}


@preprocessor("input_guardrails", order=50)
class InputGuardrails(PreProcessor):
    """Default input guardrails always run (prompt injection, SQL injection); blocked input short-circuits with a safe reply."""

    async def run(self, ctx: TurnContext) -> TurnContext:
        ctx.guard.language = ctx.language
        results = await run_guardrails(ctx.policy.defaults.input_guardrails, ctx.text, ctx)
        flagged = [(n, r) for n, r in results if r.flagged]
        if flagged:
            ctx.input_flagged = True
            log.warning("input blocked", guardrails=[n for n, _ in flagged])
            for n, r in flagged:
                ctx.decide("guardrail", "blocked_input", reason=r.reason, ref=n)
            ctx.short_circuit_with(REFUSAL.get(ctx.language, REFUSAL["en"]), note="input guardrail: " + "; ".join(r.reason for _, r in flagged))
        return ctx

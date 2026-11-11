from __future__ import annotations

import re

from keelchat.capabilities.nodes.common import affirm
from keelchat.logging import get_logger
from keelchat.registry.core import PreProcessor, preprocessor
from keelchat.runtime.context import TurnContext

log = get_logger("opt_out")

PATTERNS = (r"^\s*(stop|unsubscribe|opt[ -]?out)\s*[.!]*$", r"\bstop (messaging|texting|contacting) me\b", r"\bno m[aá]s mensajes\b")
#: asked when the model (not the STOP words) reads a wish to stop; the next reply settles it
CONFIRM_STOP = {
    "en": "Do you want me to stop here? Reply yes to stop, or no to carry on.",
    "es": "¿Quieres que me detenga aquí? Responde sí para parar, o no para seguir.",
}
CONFIRM_STOP_KEY = "confirm_stop"
CARRY_ON = {"en": "Okay, let's carry on.", "es": "De acuerdo, sigamos."}
REPLY = {
    "en": "Understood. You won't receive further messages. Reply START to continue later.",
    "es": "Entendido. No recibirás más mensajes. Responde START para continuar más tarde.",
}


@preprocessor("opt_out", order=15)
class OptOutPassthrough(PreProcessor):
    """Text-level opt-out on any channel (SMS STOP is handled by the channel before the pipeline). Ends the session."""

    async def run(self, ctx: TurnContext) -> TurnContext:
        low = ctx.text.lower()
        if any(re.search(p, low) for p in PATTERNS):  # STOP words: compliance, never a model call
            stop(ctx)
        return ctx


@preprocessor("confirm_stop", order=55)
class ConfirmStop(PreProcessor):
    """The reply to "do you want me to stop here?". Runs after the input guardrails, so a blocked message is never read
    as an answer (the question stays open for the next one); reading a reply may call the model."""

    async def run(self, ctx: TurnContext) -> TurnContext:
        if not ctx.state.data.pop(CONFIRM_STOP_KEY, False):
            return ctx
        meant = await affirm(ctx, ctx.text, CONFIRM_STOP["en"])
        if meant == "yes":
            stop(ctx)
        elif meant == "no":
            q = ctx.state.open_question
            ctx.route = "carry_on"
            ctx.short_circuit_with(CARRY_ON.get(ctx.language, CARRY_ON["en"]) + (f"\n{q.ask}" if q else ""))
        # unclear: an ordinary message, handled as usual
        return ctx


def stop(ctx: TurnContext) -> None:
    """End the session as opted out, with the fixed compliance reply."""
    ctx.state.exited = True
    ctx.state.exit_reason = "opted_out"
    ctx.state.open_question = None
    ctx.exit_reason = "opted_out"
    ctx.route = "opt_out"
    log.info("opt-out", channel=ctx.inbound.channel)
    ctx.short_circuit_with(REPLY.get(ctx.language, REPLY["en"]), note="opt-out")

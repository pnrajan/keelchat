from __future__ import annotations

import re
from typing import Any

from keelchat.logging import get_logger
from keelchat.registry.core import PreProcessor, preprocessor
from keelchat.runtime.context import TurnContext
from keelchat.runtime.llm import LLMRequest

log = get_logger("shorthand")

TRIGGERS = {"u", "ur", "r", "hav", "wrk", "2mrw", "tmrw", "nr", "pls", "plz", "thx", "b4", "d", "q", "xq", "tb", "trabjo", "k"}
SCHEMA: dict[str, Any] = {"type": "object", "properties": {"expanded": {"type": "string"}}, "required": ["expanded"], "additionalProperties": False}


def is_bare(text: str) -> bool:
    t = text.strip().lower()
    return t.isdigit() or len(t.split()) <= 1


def looks_like_shorthand(text: str) -> bool:
    return any(w in TRIGGERS for w in re.findall(r"[\w']+", text.lower()))


@preprocessor("shorthand", order=30)
class Shorthand(PreProcessor):
    timeout_s = 15.0  # fail open fast: an unexpanded message is better than a stalled turn
    """Expands SMS shorthand and typos using the open question as context. The original text is kept."""

    async def run(self, ctx: TurnContext) -> TurnContext:
        if is_bare(ctx.text) or not (looks_like_shorthand(ctx.text) or ctx.inbound.channel == "sms"):
            return ctx
        q = ctx.state.open_question
        system = (
            "Expand SMS shorthand and fix obvious typos in the user's message. Keep the language of the message; do not translate, "
            "do not answer it, do not add information." + (f' The agent just asked: "{q.ask}" (expecting: {q.expect}).' if q else "")
        )
        req = LLMRequest(
            "shorthand",
            ctx.services.models.classify,
            system,
            [{"role": "user", "content": ctx.text}],
            max_tokens=200,
            effort=ctx.services.models.effort_classify,
        )
        try:
            out = await ctx.services.llm.structured(req, SCHEMA)
            ctx.llm_calls += 1
            expanded = str(out.get("expanded") or ctx.text).strip()
            if expanded and expanded != ctx.text:
                log.info("shorthand expanded", changed=True)
                ctx.text = expanded
        except Exception as e:
            log.error("shorthand failed; using original", error=f"{e.__class__.__name__}: {e}")
        return ctx

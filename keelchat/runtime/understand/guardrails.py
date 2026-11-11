"""Shared guardrail runner (parallel, timed, isolated). Used by the input pre-processor and the Respond stage."""

from __future__ import annotations

import asyncio

from keelchat.logging import get_logger
from keelchat.registry.core import REGISTRY, GuardResult
from keelchat.runtime.context import TurnContext

log = get_logger("guardrails")


async def run_guardrails(names: list[str], text: str, ctx: TurnContext) -> list[tuple[str, GuardResult]]:
    async def one(name: str) -> tuple[str, GuardResult]:
        spec = REGISTRY.guardrail(name)
        try:
            r = await asyncio.wait_for(spec.cls().check(text, ctx.guard), timeout=spec.cls.timeout_s)
        except Exception as e:  # input guardrails fail closed, output guardrails fail open
            log.error("guardrail error", guardrail=name, error=f"{e.__class__.__name__}: {e}")
            r = GuardResult(flagged=(spec.stage == "input"), reason=f"{name} errored")
        return name, r

    return list(await asyncio.gather(*(one(n) for n in names)))

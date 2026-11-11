"""Understand = every registered pre-processor, in ascending `order`, each timed and isolated."""

from __future__ import annotations

import asyncio

from keelchat.logging import Metrics, Timer, bound, get_logger
from keelchat.registry.core import REGISTRY
from keelchat.runtime.context import StageTrace, TurnContext

log = get_logger("understand")


class UnderstandStage:
    name = "understand"

    def __init__(self, metrics: Metrics):
        self.metrics = metrics

    async def run(self, ctx: TurnContext) -> TurnContext:
        for spec in REGISTRY.preprocessors():
            label = f"understand.{spec.name}"
            with bound(stage=label):
                if ctx.short_circuit:
                    ctx.trace.append(StageTrace(label, 0.0, skipped=True, note="short_circuit"))
                    continue
                with Timer() as t:
                    try:
                        ctx = await asyncio.wait_for(spec.cls().run(ctx), timeout=spec.cls.timeout_s)
                    except Exception as e:  # a contributed pre-processor never breaks the turn
                        log.error("preprocessor failed; skipped", preprocessor=spec.name, error=f"{e.__class__.__name__}: {e}")
                        ctx.errors.append(f"preprocessor {spec.name}: {e.__class__.__name__}")
                self.metrics.record(label, t.ms)
                ctx.trace.append(StageTrace(label, round(t.ms, 3)))
        return ctx

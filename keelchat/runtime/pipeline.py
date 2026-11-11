"""Stage interface and runner. Three stages, each made of timed sub-steps; short-circuit jumps to Respond."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from keelchat.logging import Metrics, Timer, bound, get_logger
from keelchat.runtime.context import StageTrace, TurnContext
from keelchat.tracing import span

log = get_logger("pipeline")

RESPOND = "respond"


@runtime_checkable
class Stage(Protocol):
    name: str

    async def run(self, ctx: TurnContext) -> TurnContext: ...


class CompositeStage:
    """A stage made of sub-steps, each timed and traced as `stage.sub`."""

    def __init__(self, name: str, steps: Sequence[Stage], metrics: Metrics, *, respects_short_circuit: bool = True):
        self.name = name
        self.steps = list(steps)
        self.metrics = metrics
        self.respects_short_circuit = respects_short_circuit

    async def run(self, ctx: TurnContext) -> TurnContext:
        for step in self.steps:
            label = f"{self.name}.{step.name}"
            with bound(stage=label):
                if ctx.short_circuit and self.respects_short_circuit:
                    ctx.trace.append(StageTrace(label, 0.0, skipped=True, note="short_circuit"))
                    continue
                with Timer() as t, span("keelchat.step", label):
                    ctx = await step.run(ctx)
                self.metrics.record(label, t.ms)
                ctx.trace.append(StageTrace(label, round(t.ms, 3)))
        return ctx


class Pipeline:
    def __init__(self, stages: Sequence[Stage], metrics: Metrics | None = None):
        self.stages = list(stages)
        self.metrics = metrics or Metrics()

    async def run(self, ctx: TurnContext) -> TurnContext:
        for stage in self.stages:
            with bound(session_id=ctx.state.id, turn_id=ctx.turn_id, policy_hash=ctx.policy.hash, stage=stage.name):
                if ctx.short_circuit and stage.name != RESPOND:
                    ctx.trace.append(StageTrace(stage.name, 0.0, skipped=True, note="short_circuit"))
                    continue
                with Timer() as t, span("keelchat.stage", stage.name):
                    ctx = await stage.run(ctx)
                self.metrics.record(stage.name, t.ms)
                ctx.trace.append(StageTrace(stage.name, round(t.ms, 3)))
                log.info("stage done", stage_ms=round(t.ms, 2), route=ctx.route, short_circuit=ctx.short_circuit)
        return ctx

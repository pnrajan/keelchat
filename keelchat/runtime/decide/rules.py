"""Rules and per-step models.

A policy's `rules:` apply on every turn; a step's `rules:` apply while that step is asking. The intent detector reports
which conditions hold (a structured verdict, in the same call that reads the message); code then drops rules that a
matched rule `overrides`, records each match in the audit log, and the matched `then`s join the reply's instructions.

A step's `model: small | large` runs that step's work on one tier, and `high_stakes` (on a step or a matched rule)
writes the reply with the large model and always checks it. A plan limited to small models stays on small models."""

from __future__ import annotations

from dataclasses import replace

from keelchat.policy.compiler import CompiledStep
from keelchat.policy.models import Rule, Tier
from keelchat.runtime.context import TurnContext
from keelchat.runtime.llm import ModelConfig, small_only


def asking_step(ctx: TurnContext) -> CompiledStep | None:
    q = ctx.state.open_question
    return next((s for s in ctx.policy.steps if q is not None and s.name == q.step), None)


def active_rules(ctx: TurnContext) -> list[Rule]:
    """The agent's rules, then those of the step that is asking."""
    step = asking_step(ctx)
    return [*ctx.policy.rules, *(step.rules if step else [])]


def grounded(reported: list[dict[str, str]], message: str) -> list[str]:
    """Rule names whose quoted evidence is in the latest message: a match the model cannot point to there is dropped,
    so a rule does not fire again on a later message because of an earlier one."""
    text = " ".join(message.lower().split())
    return [str(m.get("name")) for m in reported if (q := " ".join(str(m.get("quote") or "").lower().split())) and q in text]


def resolve(matched: list[str], active: list[Rule]) -> list[Rule]:
    """The active rules named in `matched`, minus any that another matched rule overrides; in policy order."""
    hit = [r for r in active if r.name in set(matched)]
    beaten = {o for r in hit for o in r.overrides}
    return [r for r in hit if r.name not in beaten]


def prompt_section(rules: list[Rule]) -> str:
    """For the intent detector: the conditions to check."""
    lines = "\n".join(f"- {r.name}: {r.when}" for r in rules)
    return (
        "\nRules: list in `rules` each rule whose condition the user's latest message itself meets, with `quote`: the exact "
        "words of the latest message that meet it. Earlier messages do not count. Empty when none does.\n"
        f"{lines}"
    )


def instructions(rules: list[Rule]) -> str:
    """For whoever writes the reply: the matched rules, as instructions."""
    return "Rules that apply to this message (follow them):\n" + "\n".join(f"- {r.then}" for r in rules) if rules else ""


def tier(m: ModelConfig, choice: Tier | None) -> ModelConfig:
    """Every job on one tier: small uses the classify model, large the generate model."""
    if choice == "small":
        return small_only(m)
    if choice == "large":
        return replace(m, classify=m.generate)
    return m


def high_stakes(ctx: TurnContext, step: str | None) -> bool:
    owner = next((s for s in ctx.policy.steps if s.name == step), None)
    return bool(owner and owner.high_stakes) or any(r.high_stakes for r in ctx.rules)


def models_for(ctx: TurnContext, step: str | None) -> ModelConfig:
    """The models for work done for `step`: its tier, or the large model when the turn is high-stakes."""
    base = ctx.services.models
    if high_stakes(ctx, step):
        return tier(base, "large")
    owner = next((s for s in ctx.policy.steps if s.name == step), None)
    return tier(base, owner.model if owner else None)

"""Rules (agent and step level), overrides, and per-step model tiers."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.helpers import RecordingLLM, say
from yachat.policy.compiler import build, compile_policy
from yachat.policy.errors import PolicyValidationError
from yachat.policy.loader import load_policy
from yachat.policy.models import Rule
from yachat.runtime.decide.rules import grounded, resolve, tier
from yachat.runtime.llm import LLMClient, LLMRequest, ModelConfig
from yachat.runtime.runtime import Runtime
from yachat.testing.fakes import FakeTools

SUPPORT = (Path(__file__).parent.parent / "examples" / "support_assistant.yaml").read_text()


def with_rules(fn: Any) -> str:
    data = yaml.safe_load(SUPPORT)
    data.pop("rules", None)  # the example's own rule; each test sets the rules it checks
    fn(data)
    return yaml.safe_dump(data, sort_keys=False)


DAMAGED = {"name": "damaged_item", "when": "the customer says an item arrived damaged or broken", "then": "say a free replacement can be sent"}


def add_agent_rule(d: dict[str, Any]) -> None:
    d["rules"] = [DAMAGED]


def test_rules_compile_and_only_change_the_hash_when_present() -> None:
    plain = build(with_rules(lambda d: None))
    ruled = build(with_rules(add_agent_rule))
    assert ruled.rules[0].name == "damaged_item" and plain.rules == [] and ruled.hash != plain.hash

    def step_rules(d: dict[str, Any]) -> None:
        d["flow"][1]["form_fill"].update(
            model="small", high_stakes=True, rules=[{"name": "po", "when": "they give a PO number", "then": "ask for the order number"}]
        )

    step = build(with_rules(step_rules)).step("form_fill")
    assert step.model == "small" and step.high_stakes and step.rules[0].name == "po"


@pytest.mark.parametrize(
    "rules,path,message",
    [
        ([DAMAGED, DAMAGED], "rules[1].name", "duplicate rule"),
        ([{**DAMAGED, "overrides": ["nope"]}], "rules[0].overrides[0]", "unregistered rule"),
        ([{**DAMAGED, "overrides": ["damaged_item"]}], "rules[0].overrides[0]", "cannot override itself"),
        ([{**DAMAGED, "overrides": ["b"]}, {"name": "b", "when": "x", "then": "y", "overrides": ["damaged_item"]}], "rules", "cycle"),
        ([{**DAMAGED, "name": "Bad Name"}], "rules[0].name", ""),
    ],
)
def test_bad_rules_are_rejected_with_their_path(rules: list[dict[str, Any]], path: str, message: str) -> None:
    with pytest.raises(PolicyValidationError) as e:
        build(with_rules(lambda d: d.update(rules=rules)))
    assert any(err.path == path and message in err.message for err in e.value.errors), e.value.errors


def test_an_overriding_rule_wins_and_unmatched_or_unknown_names_are_dropped() -> None:
    a = Rule(name="a", when="w", then="t")
    b = Rule(name="b", when="w", then="t", overrides=["a"])
    c = Rule(name="c", when="w", then="t")
    assert [r.name for r in resolve(["a", "b", "zzz"], [a, b, c])] == ["b"]
    assert [r.name for r in resolve(["c", "a"], [a, b, c])] == ["a", "c"]  # policy order


def test_a_match_counts_only_when_its_quote_is_in_the_latest_message() -> None:
    reported = [{"name": "a", "quote": "arrived  BROKEN"}, {"name": "b", "quote": "cracked jug"}, {"name": "c", "quote": ""}]
    assert grounded(reported, "ORD-12345, it arrived broken") == ["a"]


def test_a_tier_puts_every_job_on_one_model() -> None:
    m = ModelConfig(generate="L", classify="S", verify="V")
    assert (tier(m, "small").generate, tier(m, "small").verifier) == ("S", "S")
    assert tier(m, "large").classify == "L" and tier(m, "large").verifier == "V"
    assert tier(m, None) is m


class TierNames(LLMClient):
    """The real model behind two tier names, so a test can see which tier each call asked for."""

    def __init__(self, real: ModelConfig) -> None:
        self.inner, self.real = RecordingLLM(), {"tier-large": real.generate, "tier-small": real.classify}
        self.calls: list[LLMRequest] = []

    def _real(self, req: LLMRequest) -> LLMRequest:
        self.calls.append(req)
        return replace(req, model=self.real.get(req.model, req.model))

    async def complete(self, req: LLMRequest) -> str:
        return await self.inner.complete(self._real(req))

    async def structured(self, req: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        return await self.inner.structured(self._real(req), schema)


async def runtime(policy_yaml: str) -> tuple[Runtime, TierNames]:
    llm = TierNames(ModelConfig.from_env())
    models = replace(ModelConfig.from_env(), generate="tier-large", classify="tier-small", verify="")
    rt = Runtime(llm, FakeTools().impls(), models=models)
    await rt.register_policy(compile_policy(load_policy(policy_yaml), models={"small": "tier-small", "large": "tier-large"}))  # pins the tier names
    return rt, llm


async def test_a_matching_rule_is_recorded_and_shapes_the_reply() -> None:
    rt, llm = await runtime(with_rules(add_agent_rule))
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    llm.calls.clear()
    r = await say(rt, s.id, "my blender arrived broken, the jug is cracked")
    assert r.rules == ["damaged_item"], r.trace
    render = [c for c in llm.calls if c.purpose == "render" and "free replacement" in c.messages[0]["content"]]
    assert render and "free replacement" in render[0].system  # the writer gets it as an instruction and beside the plain version
    assert all("free replacement" in c.messages[0]["content"] for c in llm.calls if c.purpose == "reply_verify")  # so does the checker
    # whether the reply then says it is model quality, scored by the evals (rule_followed), not asserted here
    audit = await rt.audit.query("default", session_id=s.id, kinds=["rule"])
    assert [(e.decision, e.turn_id) for e in audit] == [("damaged_item", r.turn_id)]
    calm = await say(rt, s.id, "ORD-12345")
    assert calm.rules == []


async def test_step_rules_apply_only_while_the_step_asks_and_steps_run_on_their_tier() -> None:
    def step_rules(d: dict[str, Any]) -> None:
        d["flow"][1]["form_fill"].update(
            model="large",
            rules=[
                {
                    "name": "po_number",
                    "when": "the customer gives a purchase order (PO) number instead of an order number",
                    "then": "explain that a PO number is not an order number and say where the order number is printed (the confirmation email)",
                }
            ],
        )

    rt, llm = await runtime(with_rules(step_rules))
    s = await rt.start_session("support-assistant", "u1")
    first = await say(rt, s.id, "hi, I need help with an order")
    assert first.rules == []  # the greeting step is asking: the form's rule is not active yet
    await say(rt, s.id, "yes")
    llm.calls.clear()
    r = await say(rt, s.id, "it's PO-88812 from our purchasing team")
    assert r.rules == ["po_number"]
    detect = [c for c in llm.calls if c.purpose == "intent_detect"]
    assert detect and all(c.model == "tier-large" for c in detect)  # the asking step's tier, also for reading the message


async def test_a_high_stakes_rule_writes_with_the_large_model_and_always_checks() -> None:
    def stakes(d: dict[str, Any]) -> None:
        d["flow"][1]["form_fill"]["model"] = "small"
        d["rules"] = [{**DAMAGED, "high_stakes": True}]

    rt, llm = await runtime(with_rules(stakes))
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    llm.calls.clear()
    r = await say(rt, s.id, "the lamp I ordered arrived smashed")
    assert r.rules == ["damaged_item"]
    render = [c for c in llm.calls if c.purpose == "render"]
    assert render and all(c.model == "tier-large" for c in render)
    assert [c for c in llm.calls if c.purpose == "reply_verify"], "a high-stakes reply is always checked"

from __future__ import annotations

from typing import Any

import pytest

from yachat.policy.compiler import build
from yachat.policy.errors import PolicyValidationError
from yachat.registry.core import REGISTRY, EmptyConfig, Node, NodeResult, NodeSpec, RegistryError, ToolSpec, _input_model_from_signature, node, tool
from yachat.runtime.decide.tool_gateway import ToolGateway

seen: list[dict[str, int]] = []


async def fixer(question: str, repairs: dict[str, int] | None = None) -> str:
    seen.append(dict(repairs or {}))
    return "ok"


class Fixer(Node[EmptyConfig]):
    async def act(self, ctx: Any, config: EmptyConfig) -> NodeResult:
        return NodeResult("ok")


@pytest.fixture
def capabilities(monkeypatch):
    monkeypatch.setitem(REGISTRY._nodes, "fixer_node", NodeSpec(name="fixer_node", cls=Fixer, config_model=EmptyConfig, repairs={"sql": 3, "code": 2}))
    monkeypatch.setitem(REGISTRY._nodes, "plain_node", NodeSpec(name="plain_node", cls=Fixer, config_model=EmptyConfig))
    spec = ToolSpec(name="fixer", fn=fixer, input_model=_input_model_from_signature("fixer", fixer), output_schema={}, repairs={"sql": 3, "code": 2})
    monkeypatch.setitem(REGISTRY._tools, "fixer", spec)
    seen.clear()


def policy(flow: str, limits: str = "") -> str:
    return f"agent: a\ncharter: c\n{limits}flow:\n{flow}"


def test_resolution_order(capabilities):
    steps = {s.name: s.repairs for s in build(policy("  - fixer_node\n  - plain_node\n")).steps}
    assert steps == {"fixer_node": {"sql": 3, "code": 2}, "plain_node": {"default": 2}}  # decorator, then platform default
    steps = {s.name: s.repairs for s in build(policy("  - fixer_node\n  - plain_node\n", "limits: {repairs: 1}\n")).steps}
    assert steps == {"fixer_node": {"sql": 1, "code": 1}, "plain_node": {"default": 1}}  # policy-wide
    steps = {s.name: s.repairs for s in build(policy("  - fixer_node\n  - plain_node\n", "limits: {repairs: 9}\n")).steps}
    assert steps == {"fixer_node": {"sql": 3, "code": 2}, "plain_node": {"default": 2}}  # policy-wide is capped, not an error
    step = build(policy("  - fixer_node: {repairs: {sql: 0}}\n", "limits: {repairs: 1}\n")).steps[0]
    assert step.repairs == {"sql": 0, "code": 1} and step.settings == {}  # the step wins for the stages it names
    assert build(policy("  - fixer_node: {repairs: 1}\n")).steps[0].repairs == {"sql": 1, "code": 1}


def test_step_overrides_are_checked_with_yaml_paths(capabilities):
    cases = {
        "  - fixer_node: {repairs: {sql: 5}}\n": ("flow[0].fixer_node.repairs.sql", "exceeds the node's maximum 3"),
        "  - fixer_node: {repairs: {sqll: 1}}\n": ("flow[0].fixer_node.repairs.sqll", "no repair stage"),
        "  - plain_node: {repairs: 4}\n": ("flow[0].plain_node.repairs", "exceeds the node's maximum 2"),
    }
    for flow, (path, msg) in cases.items():
        with pytest.raises(PolicyValidationError) as e:
            build(policy(flow))
        assert e.value.errors[0].path == path and msg in e.value.errors[0].message
    with pytest.raises(PolicyValidationError):
        build(policy("  - fixer_node: {repairs: -1}\n"))


def test_repairs_change_the_hash_and_show_in_the_registry(capabilities):
    assert build(policy("  - fixer_node\n")).hash != build(policy("  - fixer_node: {repairs: 1}\n")).hash
    entry = next(e for e in REGISTRY.manifest().entries if e.name == "fixer_node")
    assert entry.properties["repairs"] == {"sql": 3, "code": 2}


async def test_gateway_fills_and_caps_tool_repairs(capabilities):
    gw = ToolGateway({"fixer"})
    for asked, expected in ((None, {"sql": 3, "code": 2}), ({"sql": 1}, {"sql": 1, "code": 2}), ({"sql": 99, "code": -4}, {"sql": 3, "code": 0})):
        assert (await gw.call("fixer", {"question": "q", **({"repairs": asked} if asked is not None else {})})).ok
        assert seen[-1] == expected  # the planner can lower the budget, never raise it
    assert (await gw.call("fixer", {"question": "q", "repairs": {"bogus": 1}})).error_kind == "invalid_args"


def test_decorators_reject_bad_repairs():
    with pytest.raises(RegistryError, match="0..10"):
        node("bad_node", repairs={"sql": 11})(Fixer)
    with pytest.raises(RegistryError, match="takes `repairs"):

        @tool("bad_tool", repairs={"sql": 1})
        async def bad_tool(q: str) -> str:
            return q

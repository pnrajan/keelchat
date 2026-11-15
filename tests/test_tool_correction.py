"""Wrong tool calls correct themselves: the gateway says what was wrong, the planner fixes the call, code counts how often."""

from __future__ import annotations

from typing import Any

from keelchat.registry.core import REGISTRY, tool
from keelchat.runtime.decide.tool_gateway import ToolGateway
from keelchat.runtime.llm import LLMClient, LLMRequest
from tests.helpers import RecordingLLM, make_runtime, say


@tool("audit_shipping_quote", timeout_s=2.0)
async def shipping_quote(weight_kg: float, country: str) -> dict[str, float]:
    """Shipping price for a parcel of `weight_kg` to `country` (a two-letter code)."""
    return {"price": round(4.0 + weight_kg * 2.5, 2)}


async def test_a_rejected_call_says_what_to_fix() -> None:
    gw = ToolGateway({"audit_shipping_quote"}, {})
    bad = await gw.call("audit_shipping_quote", {"weight": "two kilos"})
    assert not bad.ok and bad.error_kind == "invalid_args"
    assert "weight_kg" in bad.fix and "country" in bad.fix and "required" in bad.fix  # names each problem
    assert "fix" in bad.as_prompt_data() and "weight_kg" in bad.as_prompt_data()  # the planner sees it
    unknown = await gw.call("shipping_price", {})
    assert unknown.error_kind == "unknown" and "audit_shipping_quote" in unknown.fix  # what it can call instead
    ok = await gw.call("audit_shipping_quote", {"weight_kg": 2, "country": "FR"})
    assert ok.ok and ok.fix == "" and ok.data == {"price": 9.0}


class Scripted(LLMClient):
    """The planner's tool choices are scripted (so the wrong call is certain); every other request goes to the real
    model. `plans` is consumed in order; when it runs out the planner answers."""

    def __init__(self, plans: list[dict[str, Any]]):
        self.inner = RecordingLLM()
        self.plans = plans
        self.plan_prompts: list[str] = []

    async def complete(self, req: LLMRequest) -> str:
        return await self.inner.complete(req)

    async def structured(self, req: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        if req.purpose == "plan":
            self.plan_prompts.append(req.system)
            return self.plans.pop(0) if self.plans else {"action": "answer", "tool": None, "args": {}, "rationale": "done"}
        return await self.inner.structured(req, schema)


POLICY = """agent: shipping-helper
charter: "Answer questions about shipping prices for our shop. Out of scope: anything else."
tools: [audit_shipping_quote]
mode: planner
"""


def plan(**args: Any) -> dict[str, Any]:
    return {"action": "tool", "tool": "audit_shipping_quote", "args": args, "rationale": "price"}


async def test_the_planner_corrects_a_wrong_call_and_answers() -> None:
    llm = Scripted([plan(weight="2kg"), plan(weight_kg=2, country="FR")])
    rt, _ = await make_runtime(POLICY, llm=llm)
    s = await rt.start_session("shipping-helper", "u1")
    r = await say(rt, s.id, "How much to ship 2 kg to France?")
    assert "fix" in llm.plan_prompts[1] and "weight_kg" in llm.plan_prompts[1]  # the second plan saw what to fix
    assert r.tool_calls == 2 and "9" in r.outbound.text, r.outbound.text


async def test_corrections_are_counted_in_code_and_the_tool_is_dropped() -> None:
    from keelchat.policy.defaults import DEFAULTS

    wrong = [plan(weight=f"heavy {i}") for i in range(6)]  # different each time (the same call twice means: answer)
    llm = Scripted(wrong)
    rt, _ = await make_runtime(POLICY, llm=llm)
    s = await rt.start_session("shipping-helper", "u1")
    r = await say(rt, s.id, "How much to ship a parcel to France?")
    assert r.tool_calls == DEFAULTS.tool_corrections + 1  # the first try and its corrections, then no more calls
    assert "audit_shipping_quote" not in llm.plan_prompts[-1].split("Use only these tools:")[1].split("Tool outputs")[0]


async def test_the_planner_never_runs_a_write_tool_even_when_asked_for_one() -> None:
    @tool("audit_cancel_order", side_effects="write")
    async def cancel(order_id: str) -> dict[str, str]:
        """Cancels an order."""
        return {"cancelled": order_id}

    try:
        policy = POLICY.replace("tools: [audit_shipping_quote]", "tools: [audit_shipping_quote, audit_cancel_order]")
        llm = Scripted([{"action": "tool", "tool": "audit_cancel_order", "args": {"order_id": "A1"}, "rationale": "x"}])
        rt, tools = await make_runtime(policy, llm=llm)
        s = await rt.start_session("shipping-helper", "u1")
        r = await say(rt, s.id, "How much to ship 2 kg to France?")  # in scope; the scripted plan tries the write tool
        assert r.tool_calls == 0 and "not one you can call" in llm.plan_prompts[1]
    finally:
        REGISTRY._tools.pop("audit_cancel_order", None)

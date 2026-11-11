"""Workspace plans are enforced in code: a used-up plan gets a fixed reply with no model call, answered turns are
counted, and a small-models plan never runs the large model."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from keelchat.api.app import create_app
from keelchat.api.keys import InMemoryApiKeyStore
from keelchat.runtime.llm import ModelConfig
from keelchat.runtime.usage import LIMIT_REACHED, Usage, charge_usd
from keelchat.state.store import InMemoryBudgetStore, InMemoryEmbedStore, InMemoryTenantToolStore, Plan
from tests.helpers import RecordingLLM, make_runtime, say


async def test_turns_are_counted_and_a_used_up_plan_calls_no_model(policy_yaml):
    budgets, llm = InMemoryBudgetStore(), RecordingLLM()
    rt, _ = await make_runtime(policy_yaml, llm=llm, budgets=budgets)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")  # no plan: unlimited, nothing tracked
    assert await budgets.get("default") is None and llm.calls

    await budgets.set_plan("default", limit_turns=2, limit_usd=None, small_models=False)
    await say(rt, s.id, "yes")
    await rt.drain()  # background memory writes are charged too
    plan = await budgets.get("default")
    assert plan is not None and plan.turns == 1 and plan.spent_usd > 0
    await say(rt, s.id, "backend engineer in austin")
    await rt.drain()
    llm.calls.clear()
    r = await say(rt, s.id, "1")  # the 3rd turn on a 2-turn plan
    assert r.outbound.text == LIMIT_REACHED and not llm.calls and r.llm_calls == 0
    assert (await budgets.get("default")).turns == 2  # type: ignore[union-attr]  # a refused turn is not counted


async def test_a_small_models_plan_never_runs_the_large_model(policy_yaml):
    budgets, llm = InMemoryBudgetStore(), RecordingLLM()
    models = ModelConfig.from_env()
    large = "large-model-that-must-not-run"
    rt, _ = await make_runtime(policy_yaml, llm=llm, budgets=budgets, models=ModelConfig(generate=large, classify=models.classify))
    await budgets.set_plan("default", limit_turns=None, limit_usd=None, small_models=True)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello, I need a job")
    await rt.drain()
    assert llm.calls and {c.model for c in llm.calls} == {models.classify}


def test_a_plan_is_used_up_by_either_limit():
    assert not Plan().exhausted()  # no limits
    assert Plan(limit_turns=100, turns=100).exhausted() and not Plan(limit_turns=100, turns=99).exhausted()
    assert Plan(limit_usd=0.25, spent_usd=0.25).exhausted() and not Plan(limit_turns=100, limit_usd=0.25, spent_usd=0.1).exhausted()


def test_a_call_with_no_known_price_is_never_free(monkeypatch):
    monkeypatch.setenv("KEELCHAT_UNPRICED_USD_PER_MTOK", "10")
    calls = [Usage("m", "render", 600_000, 400_000, cost_usd=None), Usage("m", "render", 10, 10, cost_usd=0.5)]
    assert charge_usd(calls) == 10.5


async def test_workspace_model_features_are_refused_once_the_plan_is_used_up(policy_yaml):
    budgets, llm = InMemoryBudgetStore(), RecordingLLM()
    rt, _ = await make_runtime(policy_yaml, llm=llm, budgets=budgets)
    await budgets.set_plan("default", limit_turns=0, limit_usd=None, small_models=True)
    app = create_app(rt, tool_store=InMemoryTenantToolStore(), embeds=InMemoryEmbedStore(), keys=InMemoryApiKeyStore())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/agents/draft", json={"description": "a survey about our coffee shop", "tenant": "default"})
        assert r.status_code == 402 and "usage limit" in r.json()["detail"]
        assert (await c.post("/v1/tenants/default/metrics/ask", json={"question": "how many sessions?"})).status_code == 402
    assert not llm.calls


async def test_usage_starts_again_each_month(monkeypatch):
    """The free plan is 100 replies (and its spend cap) every month: a new month starts from zero."""
    from keelchat.state import store

    budgets = InMemoryBudgetStore()
    monkeypatch.setattr(store, "usage_period", lambda: "2026-11")
    await budgets.set_plan("default", limit_turns=100, limit_usd=0.25, small_models=True)
    await budgets.charge("default", 0.25, turns=100)
    assert (await budgets.get("default")).exhausted()
    monkeypatch.setattr(store, "usage_period", lambda: "2026-12")
    plan = await budgets.get("default")
    assert plan.turns == 0 and plan.spent_usd == 0 and not plan.exhausted()
    await budgets.charge("default", 0.01, turns=1)
    assert (await budgets.get("default")).turns == 1

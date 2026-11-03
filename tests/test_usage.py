from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import make_runtime, say
from yachat.runtime import usage
from yachat.runtime.audit import usage_report
from yachat.state.models import AuditEvent


def test_config_prices_win_and_unknown_models_have_no_cost(monkeypatch):
    monkeypatch.setenv("YACHAT_MODEL_PRICES", '{"house-model": [2, 10]}')
    usage.price_overrides.cache_clear()
    try:
        assert usage.price("house-model", 1_000_000, 100_000) == (3.0, "config")
        assert usage.price("no-such-model-anywhere", 10, 10) == (None, "unknown")
    finally:
        usage.price_overrides.cache_clear()


def test_the_report_groups_tokens_and_cost():
    now = datetime.now(UTC)

    def call(model: str, purpose: str, cost: float | None, agent: str = "a", sid: str = "s1") -> AuditEvent:
        return AuditEvent(
            tenant="t",
            agent=agent,
            session_id=sid,
            kind="llm",
            decision="call",
            ref=model,
            reason=purpose,
            input_tokens=1000,
            output_tokens=100,
            cached_tokens=200,
            cost_usd=cost,
            ms=500.0,
            at=now,
        )

    events = [
        AuditEvent(tenant="t", agent="a", session_id="s1", kind="turn", decision="form_fill", at=now),
        AuditEvent(tenant="t", agent="b", session_id="s2", kind="turn", decision="form_fill", at=now),
        call("m1", "intent", 0.01),
        call("m1", "rewrite", 0.03),
        call("m2", "intent", None, agent="b", sid="s2"),
    ]
    r = usage_report(events, now - timedelta(days=1), now)
    assert r.total.calls == 3 and r.total.input_tokens == 3000 and r.total.output_tokens == 300 and r.total.cached_tokens == 600
    assert r.total.cost_usd == 0.04 and r.total.unpriced_calls == 1 and r.cost_per_conversation == 0.02
    assert [(x.key, x.cost_usd) for x in r.by_model] == [("m1", 0.04), ("m2", None)]
    assert {x.key: x.calls for x in r.by_purpose} == {"intent": 2, "rewrite": 1} and len(r.by_day) == 2 and r.by_day[-1].calls == 3


async def test_every_model_call_of_a_turn_is_recorded(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    r = await say(rt, s.id, "hello, I'm looking for work in Austin")
    await rt.drain()
    calls = await rt.audit.query("default", session_id=s.id, kinds=["llm"])
    assert len([c for c in calls if c.turn_id == r.turn_id]) >= r.llm_calls >= 1
    assert all(c.ref and c.reason and (c.input_tokens or 0) > 0 and (c.output_tokens or 0) > 0 and c.ms for c in calls)
    assert {c.ref for c in calls} <= {rt.models.generate, rt.models.classify, rt.models.verifier}


async def test_usage_over_the_api_is_per_tenant(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from yachat.api.app import create_app
    from yachat.runtime.llm import ModelConfig
    from yachat.runtime.llm_factory import llm_from_env
    from yachat.runtime.runtime import Runtime
    from yachat.state.store import InMemoryTenantToolStore
    from yachat.testing.fakes import FakeTools

    rt = Runtime(llm_from_env(), FakeTools().impls(), models=ModelConfig.from_env())
    now = datetime.now(UTC)

    def mk(tenant: str, cost: float) -> AuditEvent:
        return AuditEvent(
            tenant=tenant,
            agent="a",
            session_id="s",
            kind="llm",
            decision="call",
            ref="m",
            reason="intent",
            input_tokens=10,
            output_tokens=5,
            cost_usd=cost,
            at=now,
        )

    await rt.audit.append([mk("acme", 0.5), mk("other", 9.0)])
    monkeypatch.setenv("YACHAT_TENANT_KEYS", "acme:k-acme")
    app = create_app(rt, tool_store=InMemoryTenantToolStore())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", headers={"authorization": "Bearer k-acme"}) as c:
        u = (await c.get("/v1/tenants/acme/usage", params={"days": 7})).json()
        assert u["total"]["cost_usd"] == 0.5 and u["models"]["generate"] and set(u["prices"]) >= set(u["models"].values())
        assert (await c.get("/v1/tenants/other/usage")).status_code == 403


def test_openrouter_prices_match_our_model_names():
    usage.load_openrouter(
        [
            {"id": "qwen/qwen3.5-35b-a3b", "pricing": {"prompt": "0.0000001", "completion": "0.0000004"}},
            {"id": "anthropic/claude-opus-5.5", "pricing": {"prompt": "0.000005", "completion": "0.000025", "input_cache_read": "0.0000005"}},
            {"id": "anthropic/claude-opus-5.5:batch", "pricing": {"prompt": "0.0000025", "completion": "0.0000125"}},
            {"id": "openrouter/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
        ]
    )
    try:
        assert usage.price("siliconflow/Qwen/Qwen3.5-35B-A3B", 1_000_000, 1_000_000) == (0.5, "openrouter")
        # cached input billed at the cache-read rate: 800k fresh at $5/M + 200k cached at $0.5/M
        assert usage.price("claude-opus-5-5", 1_000_000, 0, cached_tokens=200_000) == (4.1, "openrouter")
        assert usage.price("openrouter/auto", 10, 10)[1] != "openrouter"
    finally:
        usage.load_openrouter([])


async def test_openrouter_prices_are_fetched_live():
    try:
        n = await usage.refresh_openrouter()
        assert n > 50 and usage.price("anthropic/claude-opus-5.5", 1000, 1000)[1] == "openrouter"
    finally:
        usage.load_openrouter([])


async def test_prices_can_be_refreshed_from_settings_at_most_once_a_minute(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from yachat.api.app import create_app
    from yachat.runtime.llm import ModelConfig
    from yachat.runtime.llm_factory import llm_from_env
    from yachat.runtime.runtime import Runtime
    from yachat.state.store import InMemoryTenantToolStore

    monkeypatch.setenv("YACHAT_OPENROUTER_PRICES", "off")  # only the button fetches here
    app = create_app(Runtime(llm_from_env(), models=ModelConfig.from_env()), tool_store=InMemoryTenantToolStore())
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/v1/tenants/default/usage/prices/refresh")
            assert r.status_code == 200 and r.json()["models"] > 50 and r.json()["openrouter_prices_at"]
            assert (await c.post("/v1/tenants/default/usage/prices/refresh")).status_code == 429
    finally:
        usage.load_openrouter([])
        usage._openrouter_at = 0.0


async def test_cheaper_models_are_suggested_from_the_live_catalog_and_priced_on_real_use():
    from yachat.runtime import usage

    if not await usage.refresh_openrouter():
        pytest.skip("OpenRouter unreachable")
    expensive = "openai/gpt-4o"
    options = usage.cheaper(expensive, 1_000_000, 200_000)
    assert options and all(o["saving"] is not None and o["saving"] > 0 for o in options)
    assert [o["cost_usd"] for o in options] == sorted(o["cost_usd"] for o in options)  # cheapest first
    assert all(o["model"].startswith("openrouter/") and o["model"].split("/")[1] in usage.PROVIDERS for o in options)
    listen = usage.cheaper("", 1000, 100, min_context=8_000, audio=True)
    assert listen and all(m.audio_in for m in usage._catalog if f"openrouter/{m.id}" in {o["model"] for o in listen})
    assert usage.job_of("intent_detect") == "classify" and usage.job_of("reply_verify") == "verify" and usage.job_of("render") == "generate"

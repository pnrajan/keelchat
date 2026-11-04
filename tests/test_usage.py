from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.helpers import make_runtime, say
from yachat.runtime import usage
from yachat.runtime.audit import UsageReport, audit_db, usage_report
from yachat.state.models import AuditEvent
from yachat.state.store import InMemoryAuditStore


async def report(events: list[AuditEvent], since: datetime, until: datetime) -> UsageReport:
    store = InMemoryAuditStore()
    await store.append(events)
    db = await audit_db(store, "t", since)
    try:
        return usage_report(db, since, until)
    finally:
        db.close()


def test_config_prices_win_and_unknown_models_have_no_cost(monkeypatch):
    monkeypatch.setenv("YACHAT_MODEL_PRICES", '{"house-model": [2, 10]}')
    usage.price_overrides.cache_clear()
    try:
        assert usage.price("house-model", 1_000_000, 100_000) == (3.0, "config")
        assert usage.price("no-such-model-anywhere", 10, 10) == (None, "unknown")
    finally:
        usage.price_overrides.cache_clear()


async def test_the_report_groups_tokens_and_cost():
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
        call("m1", "intent", 9.0, sid="s0").model_copy(update={"at": now - timedelta(days=3)}),  # before the window
        call("m1", "intent", 9.0).model_copy(update={"tenant": "other"}),  # another tenant
    ]
    r = await report(events, now - timedelta(days=1), now)
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


class _Resp:
    """The shape of a provider response as litellm returns it: OpenAI-style usage, and proxy headers when present."""

    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.usage = type("U", (), {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": None})()
        self._hidden_params = {"additional_headers": {"llm_provider-" + k: v for k, v in (headers or {}).items()}}


def test_proxy_headers_name_the_routed_model_its_cost_and_the_tokens_saved():
    gw = {"x-litellm-model-name": "openai/Qwen/Qwen3.5-35B-A3B", "x-litellm-response-cost": "0.0012", "x-headroom-tokens-saved": "340"}
    assert usage.proxy_report(_Resp(gw)) == ("openai/Qwen/Qwen3.5-35B-A3B", 0.0012, 340)
    # a gateway that has no price for the model reports 0: unknown, not free
    assert usage.proxy_report(_Resp({"x-litellm-response-cost-original": "0.0"})) == (None, None, 0)
    assert usage.proxy_report(object()) == (None, None, 0)


async def test_metered_records_any_clients_calls_with_the_gateways_cost(monkeypatch):
    """`@metered` on a client's provider call is all a custom LLMClient needs for the cost page."""
    from yachat.runtime.llm import LLMRequest

    class MyClient:
        @usage.metered()
        async def _send(self, req: LLMRequest, headers: dict[str, str]) -> _Resp:
            return _Resp(headers)

    req = LLMRequest("intent_detect", "large", "system", [{"role": "user", "content": "hi"}])
    with usage.metering() as calls:
        await MyClient()._send(
            req, headers={"x-litellm-model-name": "openai/Qwen/Qwen3.5-35B-A3B", "x-litellm-response-cost": "0.002", "x-headroom-tokens-saved": "120"}
        )
        monkeypatch.setenv("YACHAT_MODEL_PRICES", '{"large": [1, 1]}')  # a configured price beats the gateway's
        usage.price_overrides.cache_clear()
        await MyClient()._send(req, headers={"x-litellm-response-cost": "0.002"})
    usage.price_overrides.cache_clear()
    assert [(c.model, c.input_tokens, c.output_tokens, c.saved_tokens, c.cost_usd) for c in calls] == [
        ("large", 1000, 50, 120, 0.002),
        ("large", 1000, 50, 0, 0.00105),
    ]
    assert usage.METER.get() is None  # metering() leaves the turn's meter as it was


async def test_the_report_counts_tokens_saved_by_headroom():
    now = datetime.now(UTC)
    ev = [
        AuditEvent(tenant="t", agent="a", kind="llm", decision="call", ref="m", reason="r", input_tokens=10, output_tokens=1, saved_tokens=s, at=now)
        for s in (5, None)
    ]
    assert (await report(ev, now - timedelta(days=1), now)).total.saved_tokens == 5


@pytest.mark.skipif(not __import__("os").getenv("LITELLM_PROXY_URL"), reason="needs a running LiteLLM gateway (docker compose --profile gateway)")
async def test_calls_through_a_live_gateway_are_priced_by_the_model_it_routed_to():
    """The gateway's aliases (small, large) have no price of their own: the model it routed each call to is priced."""
    from yachat.runtime.llm import LLMRequest, ModelConfig
    from yachat.runtime.llm_factory import llm_from_env

    await usage.refresh_openrouter()
    try:
        with usage.metering() as calls:
            req = LLMRequest("intent_detect", ModelConfig.from_env().classify, "Reply in one word.", [{"role": "user", "content": "hi"}], max_tokens=10)
            await llm_from_env().complete(req)
    finally:
        usage.load_openrouter([])
    (c,) = calls
    assert c.input_tokens > 0 and c.output_tokens > 0 and c.cost_usd is not None, c

from __future__ import annotations

import pytest

from keelchat.analytics.analyst import Analyst, Limits
from keelchat.analytics.service import AnalyticsService
from keelchat.capabilities.nodes.analytics import HISTORY, reply_text
from keelchat.channels import CHANNELS
from keelchat.policy.compiler import build_file
from keelchat.registry.contract import check_capability
from keelchat.runtime.context import OutboundMessage
from keelchat.runtime.llm import ModelConfig
from keelchat.runtime.llm_factory import llm_from_env
from keelchat.runtime.runtime import Runtime
from tests.conftest import ROOT
from tests.helpers import say
from tests.test_analytics import root, sales  # noqa: F401  (fixture)

EXAMPLE = ROOT / "examples" / "sales_analyst.yaml"


def test_example_compiles_with_resolved_repairs():
    p = build_file(str(EXAMPLE), datasets={"sales": "structured"})
    assert p.datasets == ["sales"] and p.allowed_tools == ["analytics_query"]
    assert p.step("analytics").repairs == {"sql": 2, "code": 2, "interpret": 2, "verify": 2}  # limits.repairs: 2, capped per stage


async def test_analytics_query_passes_the_contract_suite():
    await check_capability("tool", "analytics_query")


def test_reply_and_channel_rendering():
    result = {"status": "ok", "summary": "US leads with 30,990.", "recommendations": [{"action": f"Do {i}"} for i in range(5)]}
    text = reply_text(result)
    assert text.startswith("US leads with 30,990.") and "3. Do 2" in text and "Do 3" not in text
    assert "rephrase" in reply_text({"status": "failed"})
    msg = OutboundMessage(text, items=[{"type": "analysis_result", "detail": True, "title": "q", "result": result}])
    assert CHANNELS["web"].render(msg)[0].payload["items"][0]["result"] == result
    for ch in ("sms", "slack", "whatsapp"):
        rendered = " ".join(m.text + str(m.payload) for m in CHANNELS[ch].render(msg))
        assert "analysis_result" not in rendered and "Choose one" not in rendered


async def test_conversation_with_follow_up(root):  # noqa: F811
    service = AnalyticsService(Analyst(llm_from_env(), ModelConfig.from_env(), Limits()))
    rt = Runtime(llm_from_env(), {"analytics_query": service.tool_impl()}, models=ModelConfig.from_env())
    await rt.datasets.put(sales(root).model_copy(update={"tenant": "default"}))
    await rt.register_policy(build_file(str(EXAMPLE), datasets={"sales": "structured"}))
    s = await rt.start_session("sales-analyst", "u1")
    r = await say(rt, s.id, "Which region brought in the most revenue?")
    assert "30" in r.outbound.text and r.outbound.items[0]["type"] == "analysis_result", r.outbound.text
    first = r.outbound.items[0]["result"]
    attempts = first["outcome"]["attempts"]
    assert first["input"]["dataset"] == "sales" and attempts["sql"] <= 3 * attempts["verify"]  # capped by limits.repairs, per round
    assert first["verification"]["plan"]["method"] and attempts["verify"] <= 3
    r = await say(rt, s.id, "And how does that look for the Pro product only?")
    assert r.outbound.items, (r.route, r.outbound.text, [t.__dict__ for t in r.trace][-6:])
    follow = r.outbound.items[0]["result"]
    assert "pro" in follow["data"]["sql"].lower() and "region" in follow["data"]["sql"].lower(), follow["data"]["sql"]
    assert len(r.state.outputs[HISTORY]) == 2 and not r.state.finished


@pytest.mark.parametrize("missing", ["dataset gone"])
async def test_deleted_dataset_is_not_available(root, missing):  # noqa: F811
    service = AnalyticsService(Analyst(llm_from_env(), ModelConfig.from_env(), Limits()))
    rt = Runtime(llm_from_env(), {"analytics_query": service.tool_impl()}, models=ModelConfig.from_env())
    await rt.register_policy(build_file(str(EXAMPLE), datasets={"sales": "structured"}))  # built, then the dataset was deleted
    s = await rt.start_session("sales-analyst", "u1")
    r = await say(rt, s.id, "Total revenue?")
    assert "can't analyse" in r.outbound.text and not r.outbound.items


async def test_off_topic_on_the_analyst_step_is_still_a_digression(root):  # noqa: F811
    calls: list[str] = []

    async def analytics_query(question, dataset, history=None, repairs=None):
        calls.append(question)
        return {"status": "ok", "summary": "ok", "recommendations": [], "input": {}, "data": {}}

    rt = Runtime(llm_from_env(), {"analytics_query": analytics_query}, models=ModelConfig.from_env())
    await rt.datasets.put(sales(root).model_copy(update={"tenant": "default"}))
    await rt.register_policy(build_file(str(EXAMPLE), datasets={"sales": "structured"}))
    s = await rt.start_session("sales-analyst", "u1")
    await say(rt, s.id, "How many orders were placed?")
    r = await say(rt, s.id, "Can you recommend a good pizza place in Paris?")
    assert calls == ["How many orders were placed?"] and r.state.digressions == 1

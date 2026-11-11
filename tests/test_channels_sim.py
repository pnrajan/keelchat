from __future__ import annotations

import yaml

from keelchat.channels import CHANNELS
from keelchat.channels.base import split_text
from keelchat.runtime.context import OutboundMessage
from keelchat.sim.harness import run_script
from tests.conftest import ROOT

LONG = " ".join(f"word{i}" for i in range(120))
ITEMS = [{"id": "j1", "title": "Backend Engineer", "location": "Austin"}, {"type": "resume_token", "token": "x"}]


def test_split_and_renderers():
    assert all(len(p) <= 50 for p in split_text(LONG, 50)) and " ".join(split_text(LONG, 50)) == LONG
    web = CHANNELS["web"].render(OutboundMessage(LONG, items=ITEMS, end_of_conversation=True))
    assert len(web) == 1 and web[0].payload["items"] == ITEMS and web[0].payload["end"]
    sms = CHANNELS["sms"].render(OutboundMessage(LONG, items=ITEMS))
    assert len(sms) > 1 and all(len(m.text) <= 160 for m in sms) and sms[0].text.startswith(f"(1/{len(sms)}) ")
    slack = CHANNELS["slack"].render(OutboundMessage("hi", items=ITEMS))
    assert slack[0].payload["blocks"][0]["type"] == "section" and "1. Backend Engineer" in slack[0].text
    wa = CHANNELS["whatsapp"].render(OutboundMessage("x" * 5000, items=ITEMS))
    assert wa[-1].payload["type"] == "interactive" and wa[-1].payload["interactive"]["action"]["sections"][0]["rows"][0]["title"] == "Backend Engineer"
    assert all(len(m.text) <= 4096 for m in wa)


def test_normalize_and_compliance():
    assert CHANNELS["web"].normalize({"text": "hi", "user_id": "u", "message_id": "m"}).channel_message_id == "m"
    assert CHANNELS["slack"].normalize({"event": {"text": "hi", "user": "U1", "ts": "1.2"}}).user_id == "U1"
    assert CHANNELS["whatsapp"].normalize({"from": "1", "id": "w", "text": {"body": "hi"}}).text == "hi"
    assert CHANNELS["sms"].compliance({"Body": "stop"}) is not None and CHANNELS["sms"].compliance({"Body": "hello"}) is None


async def test_sim_harness(policy_yaml):
    script = yaml.safe_load((ROOT / "examples" / "sim_script.yaml").read_text())
    report = await run_script(policy_yaml, script)
    assert report.exit_reasons == {"completed": 1, "max_reasks": 1, "disengaged": 1}
    by = {c.name: c for c in report.conversations}
    assert by["bad_address"].reasks >= 2 and by["disengaged"].max_digressions >= 3 and by["disengaged"].nudges == 1
    assert report.to_dict()["avg_llm_calls_per_turn"] > 0


async def test_a_model_played_persona_reaches_a_person(policy_yaml):
    from keelchat.policy.compiler import build
    from keelchat.runtime.llm import ModelConfig
    from keelchat.runtime.llm_factory import llm_from_env
    from keelchat.sim.harness import sim_runtime, simulate
    from tests.helpers import mutate

    policy = build(mutate(policy_yaml, lambda d: d.update(handoff={})))
    rt = sim_runtime(llm_from_env(), ModelConfig.from_env())
    await rt.register_policy(policy)
    persona = "A job seeker who agrees to share their answers when asked, then right away asks to talk to a real person instead of the bot."
    report = await simulate(rt, policy, [{"name": "wants_human", "persona": persona, "max_turns": 6}])
    (c,) = report.conversations
    assert c.persona == persona and 1 <= c.turns <= 6 and c.exit_reason == "handed_off", c.transcript
    assert {"kind": "handoff", "decision": "opened"}.items() <= next(d for d in c.decisions if d["kind"] == "handoff").items()
    assert report.summary is not None and report.summary.handoff_rate == 1.0 and report.to_dict()["summary"]["sessions"] == 1


async def test_simulations_over_the_api_leave_no_trace_in_the_real_log(policy_yaml):
    from httpx import ASGITransport, AsyncClient

    from keelchat.api.app import create_app
    from keelchat.runtime.llm import ModelConfig
    from keelchat.runtime.llm_factory import llm_from_env
    from keelchat.runtime.runtime import Runtime
    from keelchat.state.store import InMemoryTenantToolStore
    from keelchat.testing.fakes import FakeTools

    tools = FakeTools()
    app = create_app(Runtime(llm_from_env(), tools.impls(), models=ModelConfig.from_env()), tool_store=InMemoryTenantToolStore())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        url = "/v1/tenants/default/agents/hiring-agent/simulate"
        assert (await c.post(url, json={"conversations": [{"name": "a", "messages": ["hi"]}]})).status_code == 404
        await c.post("/v1/agents", json={"yaml": policy_yaml})
        bad = await c.post(url, json={"conversations": [{"name": "a", "messages": ["hi"], "persona": "someone curious"}]})
        assert bad.status_code == 422
        r = (await c.post(url, json={"conversations": [{"name": "happy", "messages": ["hello", "yes", "backend engineer in austin"]}]})).json()
        (conv,) = r["conversations"]
        assert conv["turns"] == 3 and any(d["kind"] == "tool" and d["ref"] == "job_search" for d in conv["decisions"])
        assert r["summary"]["sessions"] == 1 and r["policy_hash"]
        assert (await c.get("/v1/tenants/default/audit")).json() == [] and not tools.calls["job_search"]  # its own fakes and log

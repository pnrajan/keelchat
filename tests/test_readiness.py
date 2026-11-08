from __future__ import annotations

import yaml
from httpx import ASGITransport, AsyncClient

from tests.conftest import ROOT
from yachat.api.app import create_app
from yachat.runtime.llm import ModelConfig
from yachat.runtime.llm_factory import llm_from_env
from yachat.runtime.runtime import Runtime
from yachat.state.store import InMemoryAgentVersionStore, InMemoryTenantToolStore
from yachat.testing.fakes import FakeTools

#: renamed so it has no eval set under evals/: a new agent
SUPPORT = (ROOT / "examples" / "support_assistant.yaml").read_text().replace("agent: support-assistant", "agent: new-support", 1)


def edited(text: str, **changes: object) -> str:
    data = yaml.safe_load(text)
    data.update(changes)
    return yaml.safe_dump(data, sort_keys=False)


def client(impls: dict | None = None) -> AsyncClient:
    rt = Runtime(llm_from_env(), impls if impls is not None else FakeTools().impls(), models=ModelConfig.from_env(), versions=InMemoryAgentVersionStore())
    return AsyncClient(transport=ASGITransport(app=create_app(rt, tool_store=InMemoryTenantToolStore())), base_url="http://t")


def by_id(report: dict) -> dict[str, dict]:
    return {i["id"]: i for i in report["items"]}


async def test_a_new_agent_shows_what_is_missing_before_launch(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_URL", raising=False)
    async with client() as c:
        r = await c.post("/v1/tenants/default/agents/readiness", json={"yaml": SUPPORT})
        assert r.status_code == 200, r.text
        report = r.json()
        items = by_id(report)
        assert items["builds"]["status"] == "ok"
        assert items["tool:knowledge_search"]["status"] == "warning" and "sample" in items["tool:knowledge_search"]["detail"]
        assert items["saved"]["status"] == "missing" and items["saved"]["fix"] == "save"
        assert items["tests"]["status"] == "missing"
        assert report["ready"] is False and report["score"] < 100
        assert all({"id", "status", "title", "detail", "area"} <= set(i) for i in report["items"])


async def test_missing_backends_scope_and_consent_are_reported(monkeypatch):
    monkeypatch.delenv("KNOWLEDGE_URL", raising=False)
    async with client(impls={}) as c:  # no fakes, no backend URLs: the tool cannot work
        text = edited(SUPPORT, charter="Help customers.", flow=[{"form_fill": {"fields": [{"name": "email", "ask": "Email?", "expect": "an email"}]}}])
        text = edited(text, memory=[{"field": "email", "from": "form_fill.email"}])
        items = by_id((await c.post("/v1/tenants/default/agents/readiness", json={"yaml": text})).json())
        assert items["tool:knowledge_search"]["status"] == "missing" and items["tool:knowledge_search"]["blocking"]
        assert items["scope"]["status"] == "warning" and items["scope"]["fix"] == "charter"
        assert items["consent"]["status"] == "missing" and items["consent"]["fix"] == "add_step:greet_consent"


async def test_invalid_policy_reports_its_build_errors():
    async with client() as c:
        report = (await c.post("/v1/tenants/default/agents/readiness", json={"yaml": edited(SUPPORT, flow=["nope"])})).json()
        items = by_id(report)
        assert items["builds"]["status"] == "missing" and items["builds"]["blocking"] and "flow[0]" in items["builds"]["detail"]
        assert report["ready"] is False


async def test_saved_and_active_versions_count(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_URL", "http://knowledge.internal")
    async with client() as c:
        await c.post("/v1/tenants/default/agents", json={"yaml": SUPPORT})
        items = by_id((await c.post("/v1/tenants/default/agents/readiness", json={"yaml": SUPPORT})).json())
        assert items["saved"]["status"] == "ok" and items["tool:knowledge_search"]["status"] == "ok"
        changed = edited(SUPPORT, charter="Help customers with returns. Out of scope: legal advice.")
        assert by_id((await c.post("/v1/tenants/default/agents/readiness", json={"yaml": changed})).json())["saved"]["status"] == "warning"  # unsaved edits


async def test_the_blank_types_placeholder_charter_is_flagged():
    from yachat.policy.templates import BLANK

    async with client() as c:
        items = by_id((await c.post("/v1/tenants/default/agents/readiness", json={"yaml": BLANK})).json())
        assert items["scope"]["status"] == "missing" and items["scope"]["blocking"] and items["scope"]["fix"] == "charter"


async def test_consent_after_the_form_blocks_launch():
    text = edited(SUPPORT, flow=[{"form_fill": {"fields": [{"name": "issue", "ask": "What went wrong?", "expect": "a description"}]}}, "greet_consent"])
    async with client() as c:
        items = by_id((await c.post("/v1/tenants/default/agents/readiness", json={"yaml": text})).json())
        assert items["consent"]["status"] == "missing" and "form_fill" in items["consent"]["detail"]

from __future__ import annotations

import os

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from tests.conftest import ROOT
from yachat.api.app import create_app
from yachat.runtime.llm import ModelConfig
from yachat.runtime.llm_factory import llm_from_env
from yachat.runtime.runtime import Runtime
from yachat.state.store import AgentVersionStore, InMemoryAgentVersionStore, InMemoryTenantToolStore
from yachat.testing.fakes import FakeTools

SUPPORT = (ROOT / "examples" / "support_assistant.yaml").read_text()


def edited(text: str, **changes: object) -> str:
    data = yaml.safe_load(text)
    data.update(changes)
    return yaml.safe_dump(data, sort_keys=False)


def app_with(versions: AgentVersionStore):
    rt = Runtime(llm_from_env(), FakeTools().impls(), models=ModelConfig.from_env(), versions=versions)
    return create_app(rt, tool_store=InMemoryTenantToolStore())


async def check_store(store: AgentVersionStore, tenant: str) -> None:
    v1, new = await store.save(tenant, "bot", "h1", "agent: bot", "first")
    assert new and v1.version == 1 and v1.note == "first"
    same, new = await store.save(tenant, "bot", "h1", "agent: bot", "again")
    assert not new and same.version == 1  # the same build is not a new version
    v2, new = await store.save(tenant, "bot", "h2", "agent: bot\n", "second")
    assert new and v2.version == 2
    assert [v.version for v in await store.versions(tenant, "bot")] == [2, 1]  # newest first
    assert await store.active(tenant, "bot") is None  # saving does not activate
    assert (await store.activate(tenant, "bot", 2)).version == 2 and (await store.active(tenant, "bot")).version == 2  # type: ignore[union-attr]
    await store.activate(tenant, "bot", 1)
    assert (await store.active(tenant, "bot")).hash == "h1"  # type: ignore[union-attr]
    assert await store.activate(tenant, "bot", 9) is None and await store.get(tenant, "bot", 9) is None
    [summary] = await store.agents(tenant)
    assert (summary.agent, summary.active_version, summary.latest_version) == ("bot", 1, 2)
    assert (tenant, "bot", 1) in [(v.tenant, v.agent, v.version) for v in await store.all_active()]


async def test_in_memory_version_store():
    await check_store(InMemoryAgentVersionStore(), "acme")


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_version_store():
    from yachat.state.postgres_store import PostgresAgentVersionStore

    store, tenant = PostgresAgentVersionStore(url=os.environ["DATABASE_URL"]), f"t{os.getpid()}-{os.urandom(3).hex()}"
    try:
        await check_store(store, tenant)
    finally:  # the rows are test data in a shared database: an API starting on it would try to load them
        async with (await store.pool).acquire() as con:
            await con.execute("DELETE FROM agent_active WHERE tenant=$1", tenant)
            await con.execute("DELETE FROM agent_versions WHERE tenant=$1", tenant)


async def test_save_version_activate_and_roll_back():
    store = InMemoryAgentVersionStore()
    async with AsyncClient(transport=ASGITransport(app=app_with(store)), base_url="http://t") as c:
        r = await c.post("/v1/tenants/default/agents", json={"yaml": SUPPORT, "note": "first cut"})
        assert r.status_code == 201, r.text
        v1 = r.json()
        assert (v1["agent"], v1["version"], v1["active"], v1["note"]) == ("support-assistant", 1, True, "first cut")
        old = (await c.post("/v1/sessions", json={"agent": "support-assistant", "user_id": "u1"})).json()
        assert old["policy_hash"] == v1["hash"]

        r = await c.post("/v1/tenants/default/agents", json={"yaml": edited(SUPPORT, charter="Help customers with returns only."), "note": "narrower"})
        v2 = r.json()
        assert r.status_code == 201 and v2["version"] == 2 and v2["hash"] != v1["hash"]
        again = (await c.post("/v1/tenants/default/agents", json={"yaml": edited(SUPPORT, charter="Help customers with returns only.")})).json()
        assert again["version"] == 2 and again["created"] is False  # saving the same policy twice keeps one version
        new = (await c.post("/v1/sessions", json={"agent": "support-assistant", "user_id": "u2"})).json()
        assert new["policy_hash"] == v2["hash"]

        listed = (await c.get("/v1/tenants/default/agents")).json()
        assert listed == [{"agent": "support-assistant", "active_version": 2, "latest_version": 2, "updated_at": listed[0]["updated_at"]}]
        versions = (await c.get("/v1/tenants/default/agents/support-assistant/versions")).json()
        assert [(v["version"], v["active"]) for v in versions] == [(2, True), (1, False)]
        got = (await c.get("/v1/tenants/default/agents/support-assistant/versions/1")).json()
        assert got["yaml"] == SUPPORT and got["hash"] == v1["hash"]

        r = await c.post("/v1/tenants/default/agents/support-assistant/versions/1/activate")
        assert r.status_code == 200 and r.json()["active"] is True  # roll back
        back = (await c.post("/v1/sessions", json={"agent": "support-assistant", "user_id": "u3"})).json()
        assert back["policy_hash"] == v1["hash"]
        state = (await c.get(f"/v1/sessions/{new['id']}/state")).json()
        assert state["policy_hash"] == v2["hash"]  # sessions keep the version they started on
        assert (await c.post("/v1/tenants/default/agents/support-assistant/versions/7/activate")).status_code == 404


async def test_only_valid_builds_are_saved():
    store = InMemoryAgentVersionStore()
    async with AsyncClient(transport=ASGITransport(app=app_with(store)), base_url="http://t") as c:
        r = await c.post("/v1/tenants/default/agents", json={"yaml": edited(SUPPORT, flow=["greet_consent", "nope"])})
        assert r.status_code == 422 and r.json()["detail"][0]["path"] == "flow[1]"
        assert await store.agents("default") == []
        r = await c.post("/v1/agents/validate", json={"yaml": edited(SUPPORT, limits={"reasks": 0})})
        assert r.status_code == 200 and [e["path"] for e in r.json()["errors"]] == ["limits.reasks"]
        r = await c.post("/v1/agents/validate", json={"yaml": "agent: [unclosed"})
        assert r.json()["errors"][0]["path"].startswith("line 1")
        r = (await c.post("/v1/agents/validate", json={"yaml": SUPPORT})).json()
        assert r["errors"] == [] and r["agent"] == "support-assistant" and r["summary"]
        assert (await c.post("/v1/sessions", json={"agent": "support-assistant", "user_id": "u"})).status_code == 404  # validate registers nothing


async def test_saved_versions_survive_a_restart():
    store = InMemoryAgentVersionStore()  # stands in for Postgres: shared by both app instances
    async with AsyncClient(transport=ASGITransport(app=app_with(store)), base_url="http://t") as c:
        h1 = (await c.post("/v1/tenants/default/agents", json={"yaml": SUPPORT})).json()["hash"]
        await c.post("/v1/tenants/default/agents", json={"yaml": edited(SUPPORT, charter="Returns only.")})
        await c.post("/v1/tenants/default/agents/support-assistant/versions/1/activate")
    app = app_with(store)
    async with app.router.lifespan_context(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        s = (await c.post("/v1/sessions", json={"agent": "support-assistant", "user_id": "u"})).json()
        assert s["policy_hash"] == h1  # the active version was rebuilt and registered at startup


async def test_draft_a_policy_from_a_description():
    async with AsyncClient(transport=ASGITransport(app=app_with(InMemoryAgentVersionStore())), base_url="http://t") as c:
        r = await c.post(
            "/v1/agents/draft",
            json={"description": "A support bot for an online shoe store. Ask for consent, then collect the order number and the problem."},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["errors"] == [], body  # the draft is checked by the real build, and repaired from its errors
        policy = yaml.safe_load(body["yaml"])
        steps = [s if isinstance(s, str) else next(iter(s)) for s in policy["flow"]]
        assert "greet_consent" in steps and "form_fill" in steps and body["attempts"] >= 1


async def test_sessions_follow_the_shared_active_version_and_keep_their_pinned_one(policy_yaml):
    from tests.helpers import RecordingLLM, mutate, say
    from yachat.policy.compiler import build
    from yachat.runtime.llm import ModelConfig
    from yachat.runtime.runtime import Runtime
    from yachat.state.store import InMemoryAgentVersionStore, InMemorySessionStore

    versions, sessions = InMemoryAgentVersionStore(), InMemorySessionStore()  # shared by every replica (Postgres, Redis)

    def replica() -> Runtime:
        return Runtime(RecordingLLM(), {}, models=ModelConfig.from_env(), versions=versions, sessions=sessions)

    a, b = replica(), replica()
    v1_yaml = policy_yaml
    v2_yaml = mutate(policy_yaml, lambda d: d.update(charter=d["charter"] + " Be extra brief."))
    v1, _ = await a.save_version(build(v1_yaml), v1_yaml)
    s1 = await a.start_session("hiring-agent", "u1")

    # 8: saving v2 without activating it changes nothing for new sessions
    v2, _ = await a.save_version(build(v2_yaml), v2_yaml, activate=False)
    assert (await a.start_session("hiring-agent", "u2")).policy_hash == v1.hash

    # 9: activating v2 on one replica reaches the others
    await a.activate_version("default", "hiring-agent", v2.version)
    assert (await b.start_session("hiring-agent", "u3")).policy_hash == v2.hash
    # ... and a rollback too
    await b.activate_version("default", "hiring-agent", v1.version)
    assert (await a.start_session("hiring-agent", "u4")).policy_hash == v1.hash

    # 7: a fresh replica (a restart) continues a session pinned to a version that isn't active
    await a.activate_version("default", "hiring-agent", v2.version)
    r = await say(replica(), s1.id, "hello")
    assert r.state.policy_hash == v1.hash and r.outbound.text


async def test_a_policy_is_changed_as_asked_in_plain_words() -> None:
    from tests.helpers import make_runtime
    from yachat.policy.compiler import build
    from yachat.state.store import InMemoryTenantToolStore

    support = (ROOT / "examples" / "support_assistant.yaml").read_text()
    rt, _ = await make_runtime(support)
    async with AsyncClient(transport=ASGITransport(app=create_app(rt, tool_store=InMemoryTenantToolStore())), base_url="http://t") as c:
        r = (await c.post("/v1/agents/edit", json={"yaml": support, "instruction": "Also ask for their email address"})).json()
    assert r["errors"] == [] and r["changes"], r
    p = build(r["yaml"])
    fields = [f["name"] for s in p.steps for f in s.settings.get("fields") or []]
    assert p.agent == "support-assistant" and any("email" in f for f in fields) and "order_number" in fields
    assert [x.name for x in p.rules] == ["damaged_item"]  # nothing else changed
    assert r["yaml"].startswith("# A generic support assistant") and "# change:" not in r["yaml"]


def test_the_plain_summary_lists_rules_and_small_talk() -> None:
    from tests.conftest import ROOT
    from yachat.policy.compiler import build
    from yachat.policy.summary import plain_summary

    lines = plain_summary(build((ROOT / "examples" / "support_assistant.yaml").read_text()))
    assert "When the customer says an item arrived damaged or broken: say a free replacement can be sent" in " ".join(lines)
    assert any("small talk" in x for x in lines) and any(x.startswith("Passes the chat to you when") for x in lines)

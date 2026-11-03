from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_versions import SUPPORT, app_with
from yachat.policy.compiler import build
from yachat.policy.templates import builtin_templates, renamed
from yachat.state.store import InMemoryTemplateStore, TemplateStore


def test_builtin_bot_types_build_and_rename_keeps_comments():
    types = {t.id: t for t in builtin_templates()}
    assert {"blank", "hiring", "support", "analyst", "coach"} <= set(types) and all(t.builtin for t in types.values())
    for t in types.values():
        if t.id != "analyst":  # the analyst names a dataset: it builds once the tenant has one
            build(renamed(t.yaml, "my-bot"))
    out = renamed(SUPPORT, "returns-desk")
    assert build(out).agent == "returns-desk" and "# A generic support assistant" in out and out.count("agent:") == SUPPORT.count("agent:")
    with pytest.raises(ValueError, match="lowercase"):
        renamed(SUPPORT, "Returns Desk")


async def check_store(store: TemplateStore, tenant: str) -> None:
    t = await store.save(tenant, "returns", "Returns desk", "Handles returns", "agent: x\ncharter: c\n")
    assert t.id == "returns" and not t.builtin
    assert [x.id for x in await store.list(tenant)] == ["returns"] and await store.list("other") == []
    assert (await store.get(tenant, "returns")).name == "Returns desk"  # type: ignore[union-attr]
    await store.save(tenant, "returns", "Returns desk v2", "", "agent: x\ncharter: c2\n")  # same id replaces
    assert (await store.get(tenant, "returns")).yaml.endswith("c2\n")  # type: ignore[union-attr]
    assert await store.delete(tenant, "returns") and await store.get(tenant, "returns") is None


async def test_in_memory_template_store():
    await check_store(InMemoryTemplateStore(), "acme")


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_template_store():
    from yachat.state.postgres_store import PostgresTemplateStore

    store, tenant = PostgresTemplateStore(url=os.environ["DATABASE_URL"]), f"t{os.getpid()}-{os.urandom(3).hex()}"
    try:
        await check_store(store, tenant)
    finally:
        async with (await store.pool).acquire() as con:
            await con.execute("DELETE FROM policy_templates WHERE tenant=$1", tenant)


async def test_create_from_a_template_copy_an_agent_and_save_a_template():
    from yachat.state.store import InMemoryAgentVersionStore

    async with AsyncClient(transport=ASGITransport(app=app_with(InMemoryAgentVersionStore())), base_url="http://t") as c:
        types = (await c.get("/v1/tenants/default/templates")).json()
        assert {"blank", "support"} <= {t["id"] for t in types} and all("yaml" in t for t in types)

        r = await c.post("/v1/tenants/default/agents/from-template", json={"template": "support", "agent": "returns-desk"})
        assert r.status_code == 201, r.text
        assert (r.json()["agent"], r.json()["version"], r.json()["active"]) == ("returns-desk", 1, True)
        assert (await c.post("/v1/sessions", json={"agent": "returns-desk", "user_id": "u"})).status_code == 201
        dup = await c.post("/v1/tenants/default/agents/from-template", json={"template": "support", "agent": "returns-desk"})
        assert dup.status_code == 409  # a name that already has versions is never overwritten
        assert (await c.post("/v1/tenants/default/agents/from-template", json={"template": "nope", "agent": "x"})).status_code == 404

        r = await c.post("/v1/tenants/default/agents/returns-desk/copy", json={"agent": "returns-desk-eu"})
        assert r.status_code == 201 and r.json()["version"] == 1 and r.json()["note"] == "copied from returns-desk v1"
        got = (await c.get("/v1/tenants/default/agents/returns-desk-eu/versions/1")).json()
        assert "agent: returns-desk-eu" in got["yaml"] and "# A generic support assistant" in got["yaml"]

        r = await c.post(
            "/v1/tenants/default/templates", json={"id": "returns", "name": "Returns desk", "description": "Our returns bot", "agent": "returns-desk"}
        )
        assert r.status_code == 201 and r.json()["builtin"] is False
        assert "returns" in {t["id"] for t in (await c.get("/v1/tenants/default/templates")).json()}
        r = await c.post("/v1/tenants/default/agents/from-template", json={"template": "returns", "agent": "returns-desk-us"})
        assert r.status_code == 201  # a saved template is a bot type too
        assert (await c.post("/v1/tenants/default/templates", json={"id": "support", "name": "x", "agent": "returns-desk"})).status_code == 409
        assert (await c.delete("/v1/tenants/default/templates/returns")).status_code == 200
        assert (await c.delete("/v1/tenants/default/templates/support")).status_code == 409  # built-ins stay

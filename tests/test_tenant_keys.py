from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from tests.test_versions import SUPPORT, app_with

ACME, GLOBEX, ADMIN = {"Authorization": "Bearer acme-key"}, {"Authorization": "Bearer globex-key"}, {"Authorization": "Bearer admin-key"}


def scoped_app(monkeypatch):
    from keelchat.state.store import InMemoryAgentVersionStore

    monkeypatch.setenv("KEELCHAT_API_KEY", "admin-key")
    monkeypatch.setenv("KEELCHAT_TENANT_KEYS", "acme:acme-key, globex:globex-key")
    monkeypatch.setenv("DATABASE_URL", "")
    return app_with(InMemoryAgentVersionStore())


async def test_a_tenant_key_sees_only_its_tenant(monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=scoped_app(monkeypatch)), base_url="http://t") as c:
        me = (await c.get("/v1/whoami", headers=ACME)).json()
        assert (me["tenant"], me["admin"], me["scopes"]) == ("acme", False, ["build"])
        assert (await c.get("/v1/whoami", headers=ADMIN)).json() == {"tenant": "default", "admin": True}
        assert (await c.get("/v1/whoami")).status_code == 401
        assert (await c.get("/v1/tenants/acme/agents", headers=ACME)).status_code == 200
        assert (await c.get("/v1/tenants/globex/agents", headers=ACME)).status_code == 403  # another tenant's path
        r = await c.post("/v1/tenants/acme/agents", json={"yaml": SUPPORT}, headers=ACME)
        assert r.status_code == 201 and r.json()["tenant"] == "acme"
        r = await c.post("/v1/sessions", json={"agent": "support-assistant", "user_id": "u"}, headers=ACME)
        assert r.status_code == 201  # no tenant given: the key's tenant
        sid = r.json()["id"]
        assert (await c.get(f"/v1/sessions/{sid}/state", headers=GLOBEX)).status_code == 403  # another tenant's session
        assert (await c.get(f"/v1/sessions/{sid}/state", headers=ACME)).status_code == 200
        body = {"agent": "support-assistant", "user_id": "u", "tenant": "globex"}
        assert (await c.post("/v1/sessions", json=body, headers=ACME)).status_code == 403
        assert (await c.get("/v1/tenants/globex/agents", headers=ADMIN)).status_code == 200  # the admin key sees all
        assert (await c.post("/v1/agents/validate", json={"yaml": SUPPORT, "tenant": "globex"}, headers=ACME)).status_code == 403


async def test_the_mcp_endpoint_needs_a_key_and_acts_as_its_tenant(monkeypatch):
    import json

    app = scoped_app(monkeypatch)
    async with app.router.lifespan_context(app), AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        hdr = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        params = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
        assert (await c.post("/mcp", json=init, headers=hdr)).status_code == 401
        r = await c.post("/mcp", json=init, headers=hdr | ACME)
        assert r.status_code == 200, r.text
        sess = {k: v for k, v in r.headers.items() if k.lower() == "mcp-session-id"}

        def result(resp):
            text = resp.text
            data = json.loads(text.split("data:", 1)[1].strip().splitlines()[0]) if "data:" in text else resp.json()
            return data["result"]

        await c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=hdr | ACME | sess)
        tools = result(await c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, headers=hdr | ACME | sess))["tools"]
        names = {t["name"] for t in tools}
        assert {"whoami", "list_bot_types", "create_agent", "check_readiness", "save_version", "try_conversation"} <= names
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "whoami", "arguments": {}}}
        out = result(await c.post("/mcp", json=call, headers=hdr | ACME | sess))
        assert '"tenant": "acme"' in json.dumps(out) or "acme" in json.dumps(out)
        args = {"bot_type": "support", "agent": "returns-desk"}
        call = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "create_agent", "arguments": args}}
        out = result(await c.post("/mcp", json=call, headers=hdr | ACME | sess))
        assert "returns-desk" in json.dumps(out) and not out.get("isError"), out
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c2:
        assert [a["agent"] for a in (await c2.get("/v1/tenants/acme/agents", headers=ACME)).json()] == ["returns-desk"]
        assert (await c2.get("/v1/tenants/globex/agents", headers=GLOBEX)).json() == []


async def test_memory_and_metrics_stay_in_the_keys_tenant(monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=scoped_app(monkeypatch)), base_url="http://t") as c:
        r = await c.delete("/v1/users/u/memory", headers=ACME)  # no tenant given: the key's own, never "default"
        assert r.status_code == 200
        rt = c._transport.app.state.runtime  # type: ignore[attr-defined]
        seen = []
        real = rt.memory_writer.delete

        async def spy(user_id, agent=None, tenant="default"):
            seen.append(tenant)
            return await real(user_id, agent, tenant)

        rt.memory_writer.delete = spy
        await c.delete("/v1/users/u/memory", headers=ACME)
        assert (await c.delete("/v1/users/u/memory?tenant=default", headers=ACME)).status_code == 403  # another tenant, named
        assert seen == ["acme"]
        assert (await c.get("/v1/users/u/memory/export?agent=x", headers=ACME)).status_code == 200
        assert (await c.get("/v1/metrics", headers=ACME)).status_code == 403  # all tenants' figures: admin only
        assert (await c.get("/v1/metrics", headers=ADMIN)).status_code == 200


async def test_the_bearer_header_is_parsed_strictly(monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=scoped_app(monkeypatch)), base_url="http://t") as c:
        for h, code in (("bearer acme-key", 200), ("BEARER acme-key", 200), ("acme-key", 401), ("Bearer  acme-key", 401), ("Basic acme-key", 401)):
            assert (await c.get("/v1/whoami", headers={"Authorization": h})).status_code == code, h

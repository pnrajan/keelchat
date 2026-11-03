from __future__ import annotations

import os

import pytest
from httpx import ASGITransport, AsyncClient

from tests.test_versions import SUPPORT, app_with
from yachat.api.keys import ApiKeyStore, InMemoryApiKeyStore, new_key, parse_key

ADMIN = {"Authorization": "Bearer admin-key"}


def bearer(k: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {k}"}


def test_keys_are_random_and_only_their_hash_is_kept():
    a, ra = new_key("acme", "site", ["runtime"])
    b, _ = new_key("acme", "site", ["runtime"])
    assert a.startswith("chk_") and a != b and ra.id in a and "hash" in ra.model_dump() and a not in str(ra.model_dump())
    assert parse_key(a) == (ra.id, a) and parse_key("chk_short") is None and parse_key("nope") is None


async def check_store(store: ApiKeyStore, tenant: str) -> None:
    raw, rec = new_key(tenant, "ci", ["build"])
    await store.add(rec)
    assert (await store.get(rec.id)).tenant == tenant  # type: ignore[union-attr]
    assert [k.id for k in await store.list(tenant)] == [rec.id]
    await store.touch(rec.id)
    assert (await store.get(rec.id)).last_used_at is not None  # type: ignore[union-attr]
    assert await store.revoke(tenant, rec.id) and (await store.get(rec.id)).revoked_at is not None  # type: ignore[union-attr]
    assert not await store.revoke("other", rec.id)


async def test_in_memory_key_store():
    await check_store(InMemoryApiKeyStore(), "acme")


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_key_store():
    from yachat.state.postgres_store import PostgresApiKeyStore

    store, tenant = PostgresApiKeyStore(url=os.environ["DATABASE_URL"]), f"t{os.getpid()}-{os.urandom(3).hex()}"
    try:
        await check_store(store, tenant)
    finally:
        async with (await store.pool).acquire() as con:
            await con.execute("DELETE FROM api_keys WHERE tenant=$1", tenant)


def keyed_app(monkeypatch):
    from yachat.state.store import InMemoryAgentVersionStore

    monkeypatch.setenv("YACHAT_API_KEY", "admin-key")
    monkeypatch.delenv("YACHAT_TENANT_KEYS", raising=False)
    monkeypatch.setenv("DATABASE_URL", "")  # in-memory keys (the test edits them); empty so .env cannot refill it
    return app_with(InMemoryAgentVersionStore())


async def test_issue_use_scope_and_revoke_keys(monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=keyed_app(monkeypatch)), base_url="http://t") as c:
        r = await c.post("/v1/tenants/acme/keys", json={"name": "builder", "scopes": ["build"]}, headers=ADMIN)
        assert r.status_code == 201 and r.json()["key"].startswith("chk_")
        build = r.json()["key"]
        assert (await c.get("/v1/whoami", headers=bearer(build))).json() == {"tenant": "acme", "admin": False, "key": r.json()["id"], "scopes": ["build"]}
        listed = (await c.get("/v1/tenants/acme/keys", headers=bearer(build))).json()
        assert [k["name"] for k in listed] == ["builder"] and all("key" not in k and "hash" not in k for k in listed)  # never shown again

        assert (await c.post("/v1/tenants/acme/agents", json={"yaml": SUPPORT}, headers=bearer(build))).status_code == 201
        r = await c.post("/v1/tenants/acme/keys", json={"name": "website", "scopes": ["runtime"], "expires_in_days": 90}, headers=bearer(build))
        assert r.status_code == 201  # a build key may issue narrower keys for its own tenant
        site = r.json()["key"]
        assert (await c.post("/v1/tenants/acme/keys", json={"name": "x", "scopes": ["admin"]}, headers=bearer(build))).status_code == 422
        assert (await c.post("/v1/tenants/globex/keys", json={"name": "x", "scopes": ["build"]}, headers=bearer(build))).status_code == 403

        s = await c.post("/v1/sessions", json={"agent": "support-assistant", "user_id": "u"}, headers=bearer(site))
        assert s.status_code == 201  # a runtime key talks to agents
        assert (await c.post(f"/v1/sessions/{s.json()['id']}/turns", json={"text": "hi"}, headers=bearer(site))).status_code == 200
        assert (await c.post("/v1/tenants/acme/agents", json={"yaml": SUPPORT}, headers=bearer(site))).status_code == 403  # but cannot edit them
        assert (await c.post("/v1/tenants/acme/keys", json={"name": "y", "scopes": ["runtime"]}, headers=bearer(site))).status_code == 403

        r = await c.post("/v1/tenants/acme/keys", json={"name": "viewer", "scopes": ["read"]}, headers=ADMIN)
        read = r.json()["key"]
        assert (await c.get("/v1/tenants/acme/agents", headers=bearer(read))).status_code == 200
        assert (await c.post("/v1/tenants/acme/agents/readiness", json={"yaml": SUPPORT}, headers=bearer(read))).status_code == 403

        site_id = next(k["id"] for k in (await c.get("/v1/tenants/acme/keys", headers=ADMIN)).json() if k["name"] == "website")
        assert (await c.delete(f"/v1/tenants/acme/keys/{site_id}", headers=bearer(build))).status_code == 200
        assert (await c.get("/v1/whoami", headers=bearer(site))).status_code == 401  # revoked at once
        tampered = build[:-1] + ("A" if build[-1] != "A" else "B")
        assert (await c.get("/v1/whoami", headers=bearer(tampered))).status_code == 401


async def test_expired_keys_stop_working(monkeypatch):
    from datetime import UTC, datetime, timedelta

    async with AsyncClient(transport=ASGITransport(app=keyed_app(monkeypatch)), base_url="http://t") as c:
        r = await c.post("/v1/tenants/acme/keys", json={"name": "temp", "scopes": ["read"], "expires_in_days": 1}, headers=ADMIN)
        key, kid = r.json()["key"], r.json()["id"]
        assert (await c.get("/v1/whoami", headers=bearer(key))).status_code == 200
        store = c._transport.app.state.keys  # type: ignore[attr-defined]
        rec = await store.get(kid)
        rec.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        assert (await c.get("/v1/whoami", headers=bearer(key))).status_code == 401


async def test_workspace_keys_must_expire_within_a_year(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from tests.helpers import make_runtime
    from yachat.api.app import create_app
    from yachat.api.keys import InMemoryApiKeyStore
    from yachat.state.store import InMemoryEmbedStore, InMemoryTenantToolStore

    monkeypatch.setenv("YACHAT_API_KEY", "admin-key")
    from tests.conftest import ROOT

    rt, _ = await make_runtime((ROOT / "examples" / "support_assistant.yaml").read_text())
    app = create_app(rt, tool_store=InMemoryTenantToolStore(), embeds=InMemoryEmbedStore(), keys=InMemoryApiKeyStore())
    admin = {"authorization": "Bearer admin-key"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        own = (await c.post("/v1/tenants/acme/keys", json={"name": "console", "scopes": ["build"], "expires_in_days": 30}, headers=admin)).json()["key"]
        me = {"authorization": f"Bearer {own}"}
        for days in (None, 366):
            r = await c.post("/v1/tenants/acme/keys", json={"name": "x", "scopes": ["runtime"], "expires_in_days": days}, headers=me)
            assert r.status_code == 422 and "365" in r.text
        ok = await c.post("/v1/tenants/acme/keys", json={"name": "shop", "scopes": ["runtime"], "expires_in_days": 365}, headers=me)
        assert ok.status_code == 201 and ok.json()["expires_at"]
        forever = await c.post("/v1/tenants/acme/keys", json={"name": "ops", "scopes": ["build"]}, headers=admin)
        assert forever.status_code == 201 and forever.json()["expires_at"] is None  # the admin may


SECRET = "s" * 40


async def test_jwt_keys_carry_server_set_scope_and_are_still_revocable(monkeypatch):
    import time

    import jwt

    from yachat.api.keys import KEY_AUDIENCE, KEY_ISSUER, resolve

    monkeypatch.setenv("YACHAT_API_KEY", "admin-key")
    monkeypatch.setenv("YACHAT_JWT_SECRET", SECRET)
    store = InMemoryApiKeyStore()
    raw, rec = new_key("acme", "shop", ["runtime"], 30)
    await store.add(rec)
    claims = jwt.decode(raw, SECRET, algorithms=["HS256"], audience=KEY_AUDIENCE, issuer=KEY_ISSUER)
    assert (claims["sub"], claims["scope"], claims["jti"]) == ("acme", "runtime", rec.id) and claims["exp"] > time.time()
    who = await resolve(f"Bearer {raw}", store)
    assert who is not None and who.tenant == "acme" and who.scopes == frozenset({"runtime"}) and who.key_id == rec.id

    forged = [
        jwt.encode({**claims, "scope": "build"}, SECRET, algorithm="HS256"),  # re-scoped: not the issued token
        jwt.encode(claims, "x" * 40, algorithm="HS256"),  # not signed by this server
        jwt.encode({**claims, "exp": int(time.time()) - 60}, SECRET, algorithm="HS256"),  # expired
        jwt.encode({**claims, "aud": "other"}, SECRET, algorithm="HS256"),  # another audience
        jwt.encode(claims, None, algorithm="none"),  # unsigned
    ]
    for t in forged:
        assert await resolve(f"Bearer {t}", store) is None, t
    await store.revoke("acme", rec.id)
    assert await resolve(f"Bearer {raw}", store) is None  # revoked at once, before it expires

    _, never = new_key("acme", "ops", ["build"])  # a JWT always expires (a year by default)
    assert never.expires_at is not None
    monkeypatch.delenv("YACHAT_JWT_SECRET")
    opaque, orec = new_key("acme", "old", ["read"])
    await store.add(orec)
    assert opaque.startswith("chk_") and (await resolve(f"Bearer {opaque}", store)) is not None  # opaque keys still work


def test_a_short_jwt_secret_is_refused(monkeypatch):
    from yachat.api.keys import KeyConfigError

    monkeypatch.setenv("YACHAT_JWT_SECRET", "short")
    with pytest.raises(KeyConfigError, match="32"):
        new_key("acme", "x", ["read"], 1)

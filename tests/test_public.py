"""Agents published to the web: the widget, its chat page and the installable app, reached without an API key."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from tests.helpers import make_runtime
from yachat.api import public
from yachat.api.app import create_app
from yachat.state.store import InMemoryEmbedStore, InMemoryTenantToolStore

ADMIN = {"authorization": "Bearer admin-secret"}


@pytest.fixture
async def client(policy_yaml, monkeypatch):
    monkeypatch.setenv("YACHAT_API_KEY", "admin-secret")  # every other route needs a key
    monkeypatch.setenv("YACHAT_EMBED_SECRET", "test-signing-secret")
    monkeypatch.setattr(public, "_SECRET", None)
    rt, _ = await make_runtime(policy_yaml)
    app = create_app(rt, tool_store=InMemoryTenantToolStore(), embeds=InMemoryEmbedStore())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://chat.example.test") as c:
        yield c


async def publish(c: AsyncClient, **kw: object) -> dict:
    body = {"name": "site", "allowed_origins": ["https://www.example.com"], "title": "Hiring help"} | kw
    r = await c.post("/v1/tenants/default/agents/hiring-agent/embeds", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


async def test_publishing_needs_a_key_and_a_saved_agent(client):
    assert (await client.post("/v1/tenants/default/agents/hiring-agent/embeds", json={"name": "x"})).status_code == 401
    r = await client.post("/v1/tenants/default/agents/nope/embeds", json={"name": "x"}, headers=ADMIN)
    assert r.status_code == 404
    r = await client.post("/v1/tenants/default/agents/hiring-agent/embeds", json={"name": "x", "allowed_origins": ["example.com"]}, headers=ADMIN)
    assert r.status_code == 422  # a site must be an origin like https://www.example.com
    e = await publish(client)
    assert e["id"].startswith("pk_") and e["app_url"] == f"http://chat.example.test/a/{e['id']}"
    assert e["snippet"] == f'<script src="http://chat.example.test/widget.js" data-agent="{e["id"]}" async></script>'
    assert [x["id"] for x in (await client.get("/v1/tenants/default/embeds", headers=ADMIN)).json()] == [e["id"]]


async def test_visitors_chat_without_an_api_key_using_a_conversation_token(client):
    e = await publish(client)
    r = await client.post(f"/v1/public/embeds/{e['id']}/sessions", json={"visitor_id": "visitor-123456"})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["visitor_id"] == "visitor-123456"
    path = f"/v1/public/sessions/{s['session_id']}/turns"
    assert (await client.post(path, json={"text": "hello"})).status_code == 401  # no token
    other = (await client.post(f"/v1/public/embeds/{e['id']}/sessions", json={"visitor_id": "someone-else-99"})).json()
    assert (await client.post(path, json={"text": "hello"}, headers={"authorization": f"Bearer {other['token']}"})).status_code == 401
    r = await client.post(path, json={"text": "hello"}, headers={"authorization": f"Bearer {s['token']}"})
    assert r.status_code == 200, r.text
    assert r.json()["text"] and set(r.json()) == {"text", "items", "end_of_conversation", "exit_reason"}  # no state, no trace
    # the session is an ordinary one, owned by the embed's tenant, so it shows in the audit log and dashboard
    assert (await client.get(f"/v1/sessions/{s['session_id']}/state")).status_code == 401  # still needs a key


async def test_only_the_allowed_sites_can_frame_the_chat(client):
    e = await publish(client, allowed_origins=["https://www.example.com", "https://shop.example.com"])
    r = await client.get(f"/embed/{e['id']}")
    assert r.status_code == 200 and "Hiring help" in r.text
    assert r.headers["content-security-policy"] == "frame-ancestors 'self' https://www.example.com https://shop.example.com"
    assert (await client.get(f"/a/{e['id']}")).headers["content-security-policy"] == "frame-ancestors 'none'"
    page = (await client.get(f"/a/{e['id']}")).text
    assert "[hidden] { display: none !important; }" in page  # the code prompt stays hidden unless an invite needs it


async def test_a_turned_off_embed_stops_everywhere(client):
    e = await publish(client)
    s = (await client.post(f"/v1/public/embeds/{e['id']}/sessions", json={})).json()
    r = await client.post(f"/v1/tenants/default/embeds/{e['id']}", json={"enabled": False}, headers=ADMIN)
    assert r.json()["enabled"] is False
    assert (await client.get(f"/embed/{e['id']}")).status_code == 404
    assert (await client.post(f"/v1/public/embeds/{e['id']}/sessions", json={})).status_code == 404
    turn = await client.post(f"/v1/public/sessions/{s['session_id']}/turns", json={"text": "hi"}, headers={"authorization": f"Bearer {s['token']}"})
    assert turn.status_code == 404


async def test_new_conversations_are_limited_per_visitor_address_and_per_day(client):
    e = await publish(client, daily_limit=12)
    codes = [(await client.post(f"/v1/public/embeds/{e['id']}/sessions", json={})).status_code for _ in range(public.SESSIONS_PER_IP + 1)]
    assert codes[:-1] == [201] * public.SESSIONS_PER_IP and codes[-1] == 429


async def test_the_daily_limit_is_a_sliding_window():
    from yachat.state.store import InMemoryRateLimiter

    lim = InMemoryRateLimiter()
    assert [await lim.take("k", 2, 60, now=t) for t in (1.0, 2.0)] == [True, True] and not await lim.take("k", 2, 60, now=3.0)
    assert await lim.take("k", 2, 60, now=61.5)


async def test_the_installable_app_has_a_manifest_icon_and_offline_shell(client):
    e = await publish(client)
    m = (await client.get(f"/a/{e['id']}/manifest.webmanifest")).json()
    assert m["start_url"] == f"/a/{e['id']}" and m["display"] == "standalone" and m["name"] == "Hiring help"
    assert (await client.get(f"/a/{e['id']}/icon.svg")).headers["content-type"].startswith("image/svg+xml")
    sw = await client.get(f"/a/{e['id']}/sw.js")
    assert sw.status_code == 200 and "/v1/" in sw.text  # conversations are never cached
    w = await client.get("/widget.js")
    assert w.status_code == 200 and w.headers["content-type"].startswith("text/javascript") and "/embed/" in w.text


def test_tokens_are_bound_to_one_conversation_and_expire(monkeypatch):
    monkeypatch.setattr(public, "_SECRET", b"k")
    t = public.sign("ses_1", "pk_1", now=1000.0)
    with pytest.raises(HTTPException):
        public.verify(t, "ses_2")
    with pytest.raises(HTTPException):
        public.verify(t, "ses_1")  # issued at t=1000, long expired
    assert public.verify(public.sign("ses_1", "pk_1"), "ses_1") == "pk_1"
    with pytest.raises(HTTPException):
        public.verify(t[:-2] + "xx", "ses_1")


def test_the_visitor_address_comes_from_the_proxy_header_when_configured(monkeypatch):
    from starlette.requests import Request

    def req(headers: dict[str, str]) -> Request:
        raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        return Request({"type": "http", "headers": raw, "client": ("172.16.8.138", 40144)})

    monkeypatch.delenv("YACHAT_CLIENT_IP_HEADER", raising=False)
    assert public.client_ip(req({"Fly-Client-IP": "203.0.113.9"})) == "172.16.8.138"  # not trusted unless configured
    monkeypatch.setenv("YACHAT_CLIENT_IP_HEADER", "Fly-Client-IP")
    assert public.client_ip(req({"Fly-Client-IP": "203.0.113.9"})) == "203.0.113.9"
    assert public.client_ip(req({})) == "172.16.8.138"  # header missing: the connection's address


async def test_a_survey_link_in_an_order_email_carries_the_score_and_what_to_split_by(client):
    """Pasted into a Shopify or Klaviyo order email: `?score=8&product=...&source=shopify`. The score becomes the first
    answer, product and source label the response, and anything else in the link is ignored."""
    from pathlib import Path

    feedback = (Path(__file__).parents[1] / "examples" / "customer_feedback.yaml").read_text()
    assert (await client.post("/v1/agents", json={"yaml": feedback}, headers=ADMIN)).status_code in (200, 201)
    r = await client.post("/v1/tenants/default/agents/customer-feedback/embeds", json={"name": "order email", "title": "How was it?"}, headers=ADMIN)
    e = r.json()
    page = await client.get(f"/a/{e['id']}?score=8&product=Sourdough%20loaf&source=shopify&email=ana@example.com")
    assert page.status_code == 200 and '"score": 8' in page.text and '"product": "Sourdough loaf"' in page.text and "ana@example.com" not in page.text
    assert '"score": null' in (await client.get(f"/a/{e['id']}?score=99")).text
    extra = {"product": "Sourdough loaf", "source": "shopify", "email": "ana@example.com"}
    assert (await client.post(f"/v1/public/embeds/{e['id']}/sessions", json={"score": 8, "labels": extra})).status_code == 422
    s = await client.post(f"/v1/public/embeds/{e['id']}/sessions", json={"score": 8, "labels": {"product": "Sourdough loaf", "source": "shopify"}})
    assert s.status_code == 201, s.text
    st = (await client.get(f"/v1/sessions/{s.json()['session_id']}/state", headers=ADMIN)).json()
    assert st["fields"]["nps"] == 8 and st["data"]["segment"] == {"product": "Sourdough loaf", "source": "shopify"}
    bad = await client.post(f"/v1/public/embeds/{e['id']}/sessions", json={"score": 8, "labels": {"product": "<script>"}})
    st = (await client.get(f"/v1/sessions/{bad.json()['session_id']}/state", headers=ADMIN)).json()
    assert "segment" not in st["data"] or "product" not in st["data"]["segment"]

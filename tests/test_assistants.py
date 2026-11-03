"""A chatbot made from uploaded content, published to every channel, including WhatsApp (Meta's Cloud API)."""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest
from httpx import ASGITransport

from yachat.api import public
from yachat.api.app import create_app
from yachat.channels.whatsapp_cloud import FakeWhatsAppSender, text_messages
from yachat.datasets.uploads import UploadRejected, file_text, html_text
from yachat.knowledge.api import create_app as knowledge_app
from yachat.knowledge.docstore import InMemoryDocumentStore
from yachat.knowledge.embedder import FakeEmbedder
from yachat.knowledge.service import KnowledgeService, make_indexer
from yachat.knowledge.vectorstore import InMemoryIndex
from yachat.state.store import InMemoryEmbedStore, InMemoryTenantToolStore

FAQ = b"Bloom Bakery FAQ\n\nWe open at 7am every day except Monday. Gluten-free bread is baked on Fridays."


def test_content_is_turned_into_text():
    title, text = html_text("<html><head><title>Hours</title><style>p{}</style></head><body><h1>Hours</h1><p>Open 7am.</p><script>x()</script></body></html>")
    assert title == "Hours" and "Open 7am." in text and "x()" not in text and "p{}" not in text
    assert "Gluten-free" in file_text("faq.md", FAQ)
    with pytest.raises(UploadRejected, match="use PDF"):
        file_text("photo.jpg", b"\xff\xd8")
    with pytest.raises(UploadRejected, match="couldn't be read as a PDF"):
        file_text("bad.pdf", b"not a pdf")


@pytest.fixture
async def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("YACHAT_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("YACHAT_EMBED_SECRET", "test-secret")
    monkeypatch.setattr(public, "_SECRET", None)
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    service, real = knowledge_app(svc), httpx.AsyncClient

    class Routed(real):  # only calls to KNOWLEDGE_URL reach the in-process Knowledge Service; the model's go out as usual
        def __init__(self, *a, **kw):
            kw["mounts"] = {**(kw.get("mounts") or {}), "http://knowledge.internal": ASGITransport(app=service)}
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Routed)
    monkeypatch.setenv("KNOWLEDGE_URL", "http://knowledge.internal")
    from tests.helpers import RecordingLLM
    from yachat.runtime.llm import ModelConfig
    from yachat.runtime.runtime import Runtime

    rt = Runtime(RecordingLLM(), {}, models=ModelConfig.from_env())
    wa = FakeWhatsAppSender()
    app = create_app(rt, tool_store=InMemoryTenantToolStore(), embeds=InMemoryEmbedStore(), whatsapp=wa)
    c = real(transport=ASGITransport(app=app), base_url="http://chat.example.test")
    yield c, wa, svc, app
    await c.aclose()


async def test_a_chatbot_is_made_from_an_uploaded_file_and_published_everywhere(setup):
    c, wa, svc, _ = setup
    r = await c.post(
        "/v1/tenants/default/assistants",
        data={"name": "Bloom Bakery", "allowed_origins": "https://bloom-bakery.myshopify.com"},
        files=[("files", ("faq.md", FAQ, "text/markdown"))],
    )
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["agent"] == "bloom-bakery" and a["documents"] == ["faq.md"] and a["dataset"] == "kb_bloom_bakery"
    names = {ch["channel"] for ch in a["channels"]}
    assert names == {"website", "shopify", "wordpress", "app", "whatsapp", "phone"}
    shopify = next(ch for ch in a["channels"] if ch["channel"] == "shopify")
    assert shopify["value"] == a["snippet"] and any("theme.liquid" in step for step in shopify["steps"])
    assert await svc.docs.ids("default/kb_bloom_bakery")  # indexed by the Knowledge Service on registration
    # the scope check reads the charter, so every document given must be named there or its questions look off-topic
    v = (await c.get("/v1/tenants/default/agents/bloom-bakery/versions/1")).json()
    assert "faq.md" in v["yaml"], v["yaml"]

    # a visitor on the widget gets an answer from the uploaded document
    s = (await c.post(f"/v1/public/embeds/{a['id']}/sessions", json={})).json()
    auth = {"authorization": f"Bearer {s['token']}"}
    reply = (await c.post(f"/v1/public/sessions/{s['session_id']}/turns", json={"text": "When does the bakery open?"}, headers=auth)).json()
    assert "7" in reply["text"], reply


async def test_bad_content_and_private_links_are_refused(setup):
    c, *_ = setup
    assert (await c.post("/v1/tenants/default/assistants", data={"name": "Empty"})).status_code == 422
    r = await c.post("/v1/tenants/default/assistants", data={"name": "Sneaky", "urls": "http://169.254.169.254/latest/meta-data"})
    assert r.status_code == 422 and "public" in r.json()["detail"]  # the cloud metadata address is never fetched
    r = await c.post("/v1/tenants/default/assistants", data={"name": "Pic"}, files=[("files", ("cat.jpg", b"\xff\xd8", "image/jpeg"))])
    assert r.status_code == 422


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def test_a_whatsapp_number_answers_with_the_agent(setup):
    c, wa, _, app = setup
    a = (await c.post("/v1/tenants/default/assistants", data={"name": "Bloom Bakery"}, files=[("files", ("faq.md", FAQ, "text/markdown"))])).json()
    creds = {"phone_number_id": "1234567890", "access_token": "EAAG" + "x" * 30, "app_secret": "s" * 32}
    conn = (await c.post(f"/v1/tenants/default/embeds/{a['id']}/whatsapp", json=creds)).json()
    assert conn["webhook_url"].endswith(f"/v1/public/whatsapp/{a['id']}") and conn["whatsapp_connected"] is True
    assert "wa_token" not in conn and "wa_app_secret" not in conn  # secrets never returned
    hook = f"/v1/public/whatsapp/{a['id']}"
    ok = await c.get(hook, params={"hub.mode": "subscribe", "hub.verify_token": conn["verify_token"], "hub.challenge": "42"})
    assert ok.text == "42"
    assert (await c.get(hook, params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "42"})).status_code == 403
    message = {"from": "14155550100", "id": "wamid.1", "type": "text", "text": {"body": "When do you open?"}}
    update = {"entry": [{"changes": [{"value": {"messages": [message]}}]}]}
    body = json.dumps(update).encode()
    assert (await c.post(hook, content=body, headers={"x-hub-signature-256": "sha256=forged"})).status_code == 403
    r = await c.post(hook, content=body, headers={"x-hub-signature-256": sign("s" * 32, body), "content-type": "application/json"})
    assert r.status_code == 200
    import asyncio

    for _ in range(600):  # the reply is sent in the background
        if wa.outbox:
            break
        await asyncio.sleep(0.1)
    (phone_id, to, msg) = wa.outbox[0]
    assert phone_id == "1234567890" and to == "14155550100" and "7" in msg.text


def test_status_updates_and_media_are_ignored():
    upd = {"entry": [{"changes": [{"value": {"statuses": [{"id": "x"}], "messages": [{"from": "1", "type": "image"}]}}]}]}
    assert text_messages(upd) == []


async def test_snippets_use_the_public_address(setup, monkeypatch):
    c, *_ = setup  # behind a TLS-terminating proxy the request arrives as http; pasted snippets must use the public https address
    monkeypatch.setenv("YACHAT_PUBLIC_URL", "https://chat.example.com")
    a = (await c.post("/v1/tenants/default/assistants", data={"name": "Bloom Bakery"}, files=[("files", ("faq.md", FAQ, "text/markdown"))])).json()
    assert a["snippet"].startswith('<script src="https://chat.example.com/widget.js"') and a["app_url"].startswith("https://chat.example.com/a/")
    assert all("chat.example.test" not in ch["value"] for ch in a["channels"])


async def test_the_install_check_finds_the_chatbot_on_the_page(setup, monkeypatch):
    from yachat.datasets import uploads

    c, *_ = setup
    a = (
        await c.post(
            "/v1/tenants/default/assistants",
            data={"name": "Bloom Bakery", "allowed_origins": "https://bloom.example.com"},
            files=[("files", ("faq.md", FAQ, "text/markdown"))],
        )
    ).json()
    pages = {
        "https://bloom.example.com/": f"<html><body><h1>Bloom</h1>{a['snippet']}</body></html>",
        "https://bloom.example.com/empty": "<html><body><h1>Bloom</h1></body></html>",
        "https://other.example.com/": f"<html><body>{a['snippet']}</body></html>",
        "https://bloom.example.com/password": "<html><body>Enter store password</body></html>",
    }

    async def fake_fetch(url: str, agent: str = "") -> tuple[str, bytes, str, str]:
        final = "https://bloom.example.com/password" if url.endswith("/locked") else url
        return final, pages[final].encode(), "text/html", "utf-8"

    monkeypatch.setattr(uploads, "fetch_raw", fake_fetch)
    check = f"/v1/tenants/default/embeds/{a['id']}/check"
    ok = (await c.post(check, json={"url": "https://bloom.example.com/"})).json()
    assert ok["installed"] is True and ok["allowed"] is True
    missing = (await c.post(check, json={"url": "https://bloom.example.com/empty"})).json()
    assert missing["installed"] is False and "didn't find" in missing["message"]
    elsewhere = (await c.post(check, json={"url": "https://other.example.com/"})).json()
    assert elsewhere["installed"] is True and elsewhere["allowed"] is False and "other.example.com" in elsewhere["message"]
    locked = (await c.post(check, json={"url": "https://bloom.example.com/locked"})).json()
    assert locked["installed"] is False and "password" in locked["message"]
    monkeypatch.undo()
    r = await c.post(check, json={"url": "http://169.254.169.254/"})
    assert r.status_code == 422 and "public" in r.json()["detail"]


async def test_a_plain_chatbot_is_drafted_for_review_without_a_model_and_nothing_is_published(setup):
    c, *_ = setup
    r = await c.post("/v1/tenants/default/assistants", data={"name": "Bloom Bakery", "draft_only": "true"}, files=[("files", ("faq.md", FAQ, "text/markdown"))])
    d = r.json()
    assert r.status_code == 201 and d["errors"] == [] and "knowledge:" in d["yaml"]
    assert any("Answers questions from your content" in line for line in d["summary"]) and "Speaks: English." in d["summary"]
    assert "id" not in d and (await c.get("/v1/tenants/default/embeds")).json() == []  # reviewed first, published later
    pub = await c.post("/v1/tenants/default/assistants/publish", json={"name": "Bloom Bakery", "yaml": d["yaml"], "allowed_origins": ["https://bloom.example"]})
    assert pub.status_code == 201 and pub.json()["agent"] == "bloom-bakery" and pub.json()["allowed_origins"] == ["https://bloom.example"]
    bad = await c.post("/v1/tenants/default/assistants/publish", json={"name": "Bloom Bakery", "yaml": "agent: x\nflow: [nope]\n"})
    assert bad.status_code == 422


async def test_a_survey_is_drafted_from_a_type_and_the_owners_words(setup):
    c, *_ = setup
    form = {"name": "Bloom Bakery", "kind": "survey", "draft_only": "true", "languages": "en es",
            "description": "After an order, ask how likely they are to recommend us and whether the bread was fresh."}  # fmt: skip
    d = (await c.post("/v1/tenants/default/assistants", data=form)).json()
    assert d["errors"] == [], d
    assert d["agent"] == "bloom-bakery" and "survey" in d["yaml"] and d["dataset"] == ""
    assert any("Has a natural conversation" in line or "Runs a survey" in line for line in d["summary"]) and "Speaks: English, Spanish." in d["summary"]
    pub = await c.post("/v1/tenants/default/assistants/publish", json={"name": "Bloom Bakery", "yaml": d["yaml"]})
    assert pub.status_code == 201, pub.text

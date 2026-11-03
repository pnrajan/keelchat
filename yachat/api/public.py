"""Agents published to the web: the website widget, the chat page it frames, and the installable app.

Visitors have no API key. An embed's public id lets them start a conversation with that one agent, which returns a
short-lived token signed for that conversation; turns need the token. The chat page tells browsers which sites may
frame it (Content-Security-Policy frame-ancestors), so only the embed's allowed sites can show the widget. Limits
per visitor address, per conversation and per embed per day keep a public page from running up model use."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from yachat.channels import CHANNELS
from yachat.channels.whatsapp_cloud import CloudSender, WhatsAppSender, signed, text_messages, unseal
from yachat.logging import get_logger
from yachat.runtime.runtime import NotFound, PolicyRetired, Runtime
from yachat.state.models import Embed
from yachat.state.store import EmbedStore
from yachat.voice import MAX_AUDIO_BYTES, FakeSpeaker, FakeTranscriber, Speaker, Transcriber, VoiceError

log = get_logger("public")

ASSETS = Path(__file__).parent / "embed_assets"


PUBLIC_PREFIXES = ("/v1/public/", "/embed/", "/a/", "/widget.js")
TOKEN_TTL_S = 24 * 3600
_ORIGIN = re.compile(r"^https?://[a-z0-9.-]+(:\d{1,5})?$")
_VISITOR = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def clean_origins(v: list[str]) -> list[str]:
    out = []
    for o in v:
        o = o.strip().rstrip("/").lower()
        if not _ORIGIN.match(o):
            raise ValueError(f"{o!r} is not a site origin like https://www.example.com")
        out.append(o)
    return out


class EmbedIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80)
    allowed_origins: list[str] = Field(default_factory=list, max_length=20)
    title: str = Field(default="", max_length=60)
    greeting: str = Field(default="", max_length=300)
    daily_limit: int = Field(default=500, ge=1, le=100_000)

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, v: list[str]) -> list[str]:
        return clean_origins(v)


class EmbedUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    allowed_origins: list[str] | None = Field(default=None, max_length=20)
    title: str | None = Field(default=None, max_length=60)
    greeting: str | None = Field(default=None, max_length=300)
    daily_limit: int | None = Field(default=None, ge=1, le=100_000)

    @field_validator("allowed_origins")
    @classmethod
    def _origins(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else clean_origins(v)


#: labels a survey link may carry (from an order email), to split results by; nothing personal
LINK_LABELS = ("product", "source")
_LABEL = re.compile(r"^[\w .,&'()/+-]{1,60}$")


def link_labels(values: dict[str, str]) -> dict[str, str]:
    return {k: v.strip() for k, v in values.items() if k in LINK_LABELS and _LABEL.match(v.strip())}


class VisitorIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    visitor_id: str | None = None
    #: from a survey link in an order email: the score clicked (0-10) and labels to split results by
    score: int | None = Field(default=None, ge=0, le=10)
    labels: dict[str, str] = Field(default_factory=dict, max_length=len(LINK_LABELS))


class CodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=4, max_length=12)


class PublicTurnIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2_000)
    message_id: str | None = Field(default=None, max_length=80)
    #: also return the reply as audio (when a speaker is configured)
    speak: bool = False


def _secret() -> bytes:
    """YACHAT_EMBED_SECRET signs conversation tokens. Unset: a random one per process (tokens then stop working
    on restart, and do not work across replicas)."""
    global _SECRET
    if _SECRET is None:
        env = os.getenv("YACHAT_EMBED_SECRET")
        if not env:
            log.warning("YACHAT_EMBED_SECRET is not set: widget conversations end on restart and do not span replicas")
        _SECRET = (env or secrets.token_hex(32)).encode()
    return _SECRET


_SECRET: bytes | None = None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def sign(session_id: str, embed_id: str, now: float | None = None) -> str:
    body = _b64(json.dumps({"s": session_id, "e": embed_id, "x": int((now or time.time()) + TOKEN_TTL_S)}).encode())
    return f"{body}.{_b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())}"


def verify(token: str, session_id: str) -> str:
    """The embed id the token was issued for, if it is valid for this conversation; else 401."""
    try:
        body, mac = token.split(".", 1)
        good = hmac.compare_digest(mac, _b64(hmac.new(_secret(), body.encode(), hashlib.sha256).digest()))
        claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, json.JSONDecodeError):
        good, claims = False, {}
    if not good or claims.get("s") != session_id or claims.get("x", 0) < time.time():
        raise HTTPException(401, "This conversation has expired. Reload the page to start a new one.")
    return str(claims["e"])


#: per visitor address: new conversations per 10 minutes, messages per minute; per conversation: messages per hour
SESSIONS_PER_IP, TURNS_PER_IP, TURNS_PER_SESSION = 10, 20, 60


def client_ip(request: Request) -> str:
    """The visitor's address. Behind a proxy that sets a header the visitor can't forge (Fly-Client-IP on Fly.io), name
    it in YACHAT_CLIENT_IP_HEADER; otherwise every visitor would share the proxy's address and its rate limits."""
    if (header := os.getenv("YACHAT_CLIENT_IP_HEADER")) and (ip := request.headers.get(header, "").strip()):
        return ip
    return request.client.host if request.client else "unknown"


def snippet(base: str, embed: Embed) -> str:
    return f'<script src="{base}/widget.js" data-agent="{embed.id}" async></script>'


def channels(base: str, e: Embed) -> list[dict[str, Any]]:
    """Every place a published agent can go, with what to paste or set. One list for the API and the console."""
    tag = snippet(base, e)
    return [
        {
            "channel": "website",
            "label": "Any website",
            "value": tag,
            "steps": ["Paste this line just before </body> on every page that should show the chat button."],
        },
        {
            "channel": "shopify",
            "label": "Shopify",
            "value": tag,
            "steps": [
                "In Shopify admin: Online Store, then Themes, then ... next to your theme, then Edit code.",
                "Open layout/theme.liquid and paste the line just before </body>. Save.",
                "Add https://{your-store}.myshopify.com and your own domain to the agent's allowed websites.",
            ],
        },
        {
            "channel": "wordpress",
            "label": "WordPress",
            "value": tag,
            "steps": ["Add a Custom HTML block (or your theme's footer code setting) and paste the line."],
        },
        {
            "channel": "app",
            "label": "Link and installable app",
            "value": f"{base}/a/{e.id}",
            "steps": ["Share the link, or open it on a phone and choose Add to Home Screen."],
        },
        {
            "channel": "whatsapp",
            "label": "WhatsApp",
            "value": f"{base}/v1/public/whatsapp/{e.id}",
            "connected": bool(e.wa_token),
            "steps": [
                "In Meta for Developers, add WhatsApp to your app and note the phone number id, a permanent access token and the app secret.",
                "Connect them here (they are stored encrypted).",
                "In WhatsApp, then Configuration, set the callback URL to this address and the verify token shown after connecting; subscribe to messages.",
            ],
        },
        {
            "channel": "phone",
            "label": "Phone calls (Twilio)",
            "value": f"{base}/v1/public/phone/embeds/{e.id}",
            "steps": ["In Twilio, set your number's voice webhook (HTTP POST) to this address. Needs TWILIO_AUTH_TOKEN on the server."],
        },
    ]


def public_router(
    rt: Runtime,
    embeds: EmbedStore,
    voice: tuple[Transcriber, Speaker] | None = None,
    whatsapp: WhatsAppSender | None = None,
) -> APIRouter:
    router = APIRouter()
    limits = rt.limits
    transcriber, speaker = voice or (FakeTranscriber(), FakeSpeaker())

    async def live(embed_id: str) -> Embed:
        e = await embeds.get(embed_id)
        if e is None or not e.enabled:
            raise HTTPException(404, "This chat is not available.")
        return e

    def page(
        embed: Embed, mode: str, request: Request, start: str = "", verify: str = "", score: int | None = None, labels: dict[str, str] | None = None
    ) -> str:
        title = embed.title or embed.agent.replace("-", " ").capitalize()
        cfg: dict[str, Any] = {
            "id": embed.id,
            "title": title,
            "greeting": embed.greeting,
            "mode": mode,
            "start": start or f"/v1/public/embeds/{embed.id}/sessions",
            #: the server transcribes recordings; otherwise the page uses the browser's own speech recognition
            "server_stt": not (isinstance(transcriber, FakeTranscriber) and not transcriber.transcript),
            #: an identity code must be confirmed first; this is the base of its routes
            "verify": verify,
            #: a score clicked in an email (?score=): the page starts the chat with it (a link scanner never runs this)
            "score": score,
            "labels": labels or {},
        }
        html = (ASSETS / "chat.html").read_text()
        data = json.dumps(cfg).replace("<", "\\u003c")
        return html.replace("__CONFIG__", data).replace("__TITLE__", title.replace("<", "&lt;"))

    @router.get("/widget.js")
    async def widget_js() -> Response:
        return Response((ASSETS / "widget.js").read_text(), media_type="text/javascript", headers={"Cache-Control": "public, max-age=300"})

    @router.get("/embed/{embed_id}", response_class=HTMLResponse)
    async def embed_page(embed_id: str, request: Request) -> HTMLResponse:
        e = await live(embed_id)
        ancestors = " ".join(["'self'", *e.allowed_origins])
        return HTMLResponse(page(e, "embed", request), headers={"Content-Security-Policy": f"frame-ancestors {ancestors}", "Cache-Control": "no-store"})

    @router.get("/a/{embed_id}", response_class=HTMLResponse)
    async def app_page(embed_id: str, request: Request, score: str = "", product: str = "", source: str = "") -> HTMLResponse:
        """The chat as a page of its own. As a survey link in an order email it can carry the score clicked
        (`?score=8`) and what to split results by (`&product=...&source=shopify`)."""
        e = await live(embed_id)
        clicked = int(score) if re.fullmatch(r"10|[0-9]", score) else None
        html = page(e, "app", request, score=clicked, labels=link_labels({"product": product, "source": source}))
        return HTMLResponse(html, headers={"Content-Security-Policy": "frame-ancestors 'none'", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    @router.get("/a/{embed_id}/manifest.webmanifest")
    async def manifest(embed_id: str) -> JSONResponse:
        e = await live(embed_id)
        name = e.title or e.agent.replace("-", " ").capitalize()
        return JSONResponse(
            {
                "name": name,
                "short_name": name[:12],
                "start_url": f"/a/{e.id}",
                "scope": f"/a/{e.id}",
                "display": "standalone",
                "background_color": "#000000",
                "theme_color": "#000000",
                "icons": [{"src": f"/a/{e.id}/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
            },
            media_type="application/manifest+json",
        )

    @router.get("/a/{embed_id}/icon.svg")
    async def icon(embed_id: str) -> Response:
        e = await live(embed_id)
        letter = (e.title or e.agent or "c")[:1].upper().replace("<", "")
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#000"/>'
            '<text x="256" y="338" font-family="Helvetica,Arial,sans-serif" font-size="260" font-weight="700" fill="#fff" '
            f'text-anchor="middle">{letter}</text></svg>'
        )
        return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

    @router.get("/a/{embed_id}/sw.js")
    async def service_worker(embed_id: str) -> Response:
        await live(embed_id)
        return Response((ASSETS / "sw.js").read_text(), media_type="text/javascript", headers={"Cache-Control": "no-cache"})

    @router.post("/v1/public/embeds/{embed_id}/sessions", status_code=201)
    async def start(embed_id: str, body: VisitorIn, request: Request) -> dict[str, Any]:
        e = await live(embed_id)
        if not await limits.take(f"ip-sessions:{client_ip(request)}", SESSIONS_PER_IP, 600):
            raise HTTPException(429, "Too many new conversations from your network. Try again in a few minutes.")
        if not await limits.take(f"day:{e.id}", e.daily_limit, 86_400):
            raise HTTPException(429, "This chat has reached its limit for today. Please try again tomorrow.")
        visitor = body.visitor_id if body.visitor_id and _VISITOR.match(body.visitor_id) else secrets.token_urlsafe(12)
        try:
            s = await rt.start_session(e.agent, f"web:{e.id}:{visitor}", "web", tenant=e.tenant)
        except (NotFound, PolicyRetired) as err:
            log.error("embed agent unavailable", embed=e.id, agent=e.agent, error=str(err))
            raise HTTPException(503, "This chat is temporarily unavailable.") from err
        if labels := link_labels(body.labels):
            await rt.label_session(s.id, labels)
        if body.score is not None and (key := await score_key(e)) and key not in s.fields:
            await rt.prefill_answer(s.id, key, body.score)  # the score clicked in the order email
        log.info("public session", embed=e.id, session_id=s.id)
        return {"session_id": s.id, "token": sign(s.id, e.id), "visitor_id": visitor}

    async def score_key(e: Embed) -> str | None:
        """The survey's 0-10 recommend question, if the embed's agent has one."""
        from yachat.capabilities.nodes.survey_answers import survey_questions_of
        from yachat.runtime.findings import is_nps

        policy = await rt.policies.latest(e.agent, e.tenant)
        return next((q.key for q in survey_questions_of(policy.steps) if is_nps(q)), None) if policy else None

    async def source_of(source: str, st: Any) -> None:
        """Check the conversation still belongs to the live embed its token was issued for."""
        e = await live(source)
        if st.tenant != e.tenant or st.agent != e.agent:
            raise HTTPException(401, "This conversation has expired. Reload the page to start a new one.")

    async def checked(session_id: str, request: Request) -> Any:
        token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        source = verify(token, session_id)
        st = await rt.session_state(session_id)
        await source_of(source, st)
        if not await limits.take(f"ip-turns:{client_ip(request)}", TURNS_PER_IP, 60) or not await limits.take(f"session:{session_id}", TURNS_PER_SESSION, 3600):
            raise HTTPException(429, "You're sending messages quickly. Wait a moment and try again.")
        return st

    async def reply(session_id: str, st: Any, text: str, message_id: str | None, speak: bool) -> dict[str, Any]:
        payload = CHANNELS["web"].payload_from_text(text, st.user_id, message_id)
        r = await rt.turn_from_payload(session_id, payload)
        out = r.outbound
        items = [i for i in out.items if not i.get("detail")]  # analysis and verification detail is for the console
        body: dict[str, Any] = {"text": out.text, "items": items, "end_of_conversation": out.end_of_conversation, "exit_reason": out.exit_reason}
        if speak:
            try:
                audio = await speaker.speak(out.text)
            except VoiceError as e:  # the reply still arrives as text; the browser can read it aloud itself
                log.error("speech failed", session_id=session_id, error=str(e)[:300])
                audio = b""
            body["audio"] = base64.b64encode(audio).decode() if audio else None
            body["audio_type"] = speaker.mime if audio else None
        return body

    @router.post("/v1/public/sessions/{session_id}/turns")
    async def turn(session_id: str, body: PublicTurnIn, request: Request) -> dict[str, Any]:
        st = await checked(session_id, request)
        return await reply(session_id, st, body.text, body.message_id, body.speak)

    # ---- phone calls (Twilio): Twilio listens and speaks; each answer is one turn -------------------------------

    def twiml(say: str, next_url: str | None) -> Response:
        """Say `say`, then listen for the answer and post it to `next_url`; no `next_url`: say it and hang up."""
        words = escape(say)[:3000]
        if next_url is None:
            xml = f"<Response><Say>{words}</Say><Hangup/></Response>"
        else:
            action = escape(next_url, {'"': "&quot;"})
            xml = (
                f'<Response><Gather input="speech" speechTimeout="auto" actionOnEmptyResult="true" action="{action}" method="POST">'
                f'<Say>{words}</Say></Gather><Redirect method="POST">{action}</Redirect></Response>'
            )
        return Response(xml, media_type="application/xml")

    async def twilio_form(request: Request) -> dict[str, str]:
        """Twilio's form fields, after checking its signature (X-Twilio-Signature, HMAC-SHA1 over the public URL
        and the sorted fields with TWILIO_AUTH_TOKEN). Without the token, phone calls are refused."""
        token = os.getenv("TWILIO_AUTH_TOKEN")
        if not token:
            raise HTTPException(503, "Phone calls are not configured.")
        form = {k: str(v) for k, v in (await request.form()).items()}
        base = os.getenv("YACHAT_PUBLIC_URL") or str(request.base_url)
        url = base.rstrip("/") + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        signed = url + "".join(k + form[k] for k in sorted(form))
        expected = base64.b64encode(hmac.new(token.encode(), signed.encode(), hashlib.sha1).digest()).decode()
        if not hmac.compare_digest(expected, request.headers.get("x-twilio-signature", "")):
            raise HTTPException(403, "invalid signature")
        return form

    def next_turn_url(session_id: str, source: str) -> str:
        return f"/v1/public/phone/turn?s={session_id}&t={sign(session_id, source)}"

    @router.post("/v1/public/phone/embeds/{embed_id}")
    async def phone_inbound(embed_id: str, request: Request) -> Response:
        """Set as a Twilio number's voice webhook: callers talk to this published agent."""
        form = await twilio_form(request)
        e = await live(embed_id)
        if not await limits.take(f"day:{e.id}", e.daily_limit, 86_400):
            return twiml("Sorry, we can't take more calls today. Please try again tomorrow.", None)
        caller = re.sub(r"[^0-9+]", "", form.get("From", ""))[:20] or secrets.token_urlsafe(8)
        s = await rt.start_session(e.agent, f"phone:{e.id}:{caller}", "web", tenant=e.tenant)
        return twiml(e.greeting or "Hello! How can I help you today?", next_turn_url(s.id, e.id))

    @router.post("/v1/public/phone/turn")
    async def phone_turn(request: Request, s: str = "", t: str = "") -> Response:
        form = await twilio_form(request)
        source = verify(t, s)
        st = await rt.session_state(s)
        await source_of(source, st)
        heard = form.get("SpeechResult", "").strip()
        if not heard:
            return twiml("Sorry, I didn't catch that. Could you say it again?", next_turn_url(s, source))
        body = await reply(s, st, heard[:2_000], form.get("CallSid"), speak=False)
        return twiml(body["text"], None if body["end_of_conversation"] else next_turn_url(s, source))

    @router.post("/v1/public/sessions/{session_id}/voice")
    async def voice_turn(session_id: str, request: Request) -> dict[str, Any]:
        """A spoken turn: the request body is the recording (Content-Type audio/wav or audio/mpeg, at most 5 MB). The
        reply comes back as text and, when a speaker is configured, audio. `heard` is what was understood."""
        st = await checked(session_id, request)
        fmt = {"audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav", "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/webm": "webm"}.get(
            (request.headers.get("content-type") or "").split(";")[0].strip().lower()
        )
        if fmt is None:
            raise HTTPException(415, "Send the recording as audio/wav or audio/mpeg.")
        audio = await request.body()
        if not audio or len(audio) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "That recording is too long. Keep it under a minute, or type instead.")
        try:
            heard = await transcriber.transcribe(audio, fmt)
        except VoiceError as e:
            log.error("transcription failed", session_id=session_id, error=str(e)[:300])
            raise HTTPException(502, "I couldn't hear that clearly. Please try again, or type your answer.") from e
        if not heard:
            raise HTTPException(422, "I didn't catch anything. Please try again, or type your answer.")
        return {"heard": heard} | await reply(session_id, st, heard[:2_000], None, speak=True)

    # ---- WhatsApp Cloud API ------------------------------------------------------------------------------------

    wa_sender = whatsapp or CloudSender()
    pending: set[asyncio.Task[None]] = set()

    @router.get("/v1/public/whatsapp/{embed_id}")
    async def whatsapp_verify(embed_id: str, request: Request) -> Response:
        """Meta's webhook check: echo hub.challenge when the verify token matches."""
        e = await live(embed_id)
        q = request.query_params
        if not e.wa_verify_token or q.get("hub.mode") != "subscribe" or not hmac.compare_digest(q.get("hub.verify_token", ""), e.wa_verify_token):
            raise HTTPException(403, "verification failed")
        return Response(q.get("hub.challenge", ""), media_type="text/plain")

    @router.post("/v1/public/whatsapp/{embed_id}")
    async def whatsapp_message(embed_id: str, request: Request) -> dict[str, str]:
        """Messages from WhatsApp, signed by Meta. Answered in the background; Meta only needs a quick 200."""
        e = await live(embed_id)
        if not e.wa_token:
            raise HTTPException(404, "WhatsApp is not connected for this agent")
        body = await request.body()
        try:
            app_secret, token = unseal(e.wa_app_secret), unseal(e.wa_token)
        except Exception as err:  # a rotated YACHAT_EMBED_SECRET: reconnect WhatsApp
            log.error("whatsapp credentials unreadable", embed=e.id, error=err.__class__.__name__)
            raise HTTPException(503, "reconnect WhatsApp for this agent") from err
        if not signed(app_secret, body, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(403, "invalid signature")
        try:
            update = json.loads(body)
        except ValueError as err:
            raise HTTPException(400, "not JSON") from err
        for m in text_messages(update):
            task = asyncio.create_task(whatsapp_answer(e, token, m))
            pending.add(task)
            task.add_done_callback(pending.discard)
        return {"status": "ok"}

    async def whatsapp_answer(e: Embed, token: str, m: dict[str, Any]) -> None:
        sender_id = re.sub(r"[^0-9]", "", str(m["from"]))[:20]
        if not await limits.take(f"wa:{e.id}:{sender_id}", TURNS_PER_IP, 60):
            return
        try:
            s = await rt.start_session(e.agent, f"wa:{e.id}:{sender_id}", "whatsapp", tenant=e.tenant)
            r = await rt.turn_from_payload(s.id, {"from": sender_id, "id": m.get("id"), "text": {"body": str(m["text"]["body"])[:2_000]}})
            for msg in r.rendered:
                await wa_sender.send(e.wa_phone_number_id, token, sender_id, msg)
        except Exception as err:  # logged; the person can write again
            log.error("whatsapp reply failed", embed=e.id, error=f"{err.__class__.__name__}: {str(err)[:200]}")

    router.whatsapp_pending = pending  # type: ignore[attr-defined]  # tests wait for background replies

    return router

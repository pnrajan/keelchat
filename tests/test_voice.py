"""Voice (ported from Verlox): spoken turns in the widget and app, and phone calls through Twilio."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re

import pytest
from httpx import ASGITransport, AsyncClient

from tests.helpers import make_runtime
from yachat.api import public
from yachat.api.app import create_app
from yachat.state.store import InMemoryEmbedStore, InMemoryTenantToolStore
from yachat.voice import FakeTranscriber, Speaker, VoiceError

BASE = "http://chat.example.test"


class Tone(Speaker):
    async def speak(self, text: str) -> bytes:
        return b"ID3-fake-mp3:" + text[:10].encode()


class Down(FakeTranscriber):
    async def transcribe(self, audio: bytes, fmt: str) -> str:
        raise VoiceError("provider down")


@pytest.fixture
async def setup(policy_yaml, monkeypatch):
    monkeypatch.setenv("YACHAT_EMBED_SECRET", "s")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-secret")
    monkeypatch.setattr(public, "_SECRET", None)
    rt, _ = await make_runtime(policy_yaml)

    async def make(transcriber):
        app = create_app(rt, tool_store=InMemoryTenantToolStore(), embeds=InMemoryEmbedStore(), voice=(transcriber, Tone()))
        c = AsyncClient(transport=ASGITransport(app=app), base_url=BASE)
        e = (await c.post("/v1/tenants/default/agents/hiring-agent/embeds", json={"name": "x", "greeting": "Hi! Ready?"})).json()
        return c, e

    return make


async def session(c, e):
    s = (await c.post(f"/v1/public/embeds/{e['id']}/sessions", json={})).json()
    return s["session_id"], {"authorization": f"Bearer {s['token']}"}


async def test_a_spoken_answer_is_transcribed_answered_and_spoken_back(setup):
    c, e = await setup(FakeTranscriber("hello there"))
    sid, auth = await session(c, e)
    r = await c.post(f"/v1/public/sessions/{sid}/voice", content=b"RIFF....WAVE", headers=auth | {"content-type": "audio/wav"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["heard"] == "hello there" and body["text"] and base64.b64decode(body["audio"]).startswith(b"ID3") and body["audio_type"] == "audio/mpeg"
    assert '"server_stt": true' in (await c.get(f"/embed/{e['id']}")).text
    await c.aclose()


async def test_voice_needs_the_conversation_token_and_an_audio_type(setup):
    c, e = await setup(FakeTranscriber("hi"))
    sid, auth = await session(c, e)
    assert (await c.post(f"/v1/public/sessions/{sid}/voice", content=b"x", headers={"content-type": "audio/wav"})).status_code == 401
    assert (await c.post(f"/v1/public/sessions/{sid}/voice", content=b"x", headers=auth | {"content-type": "text/plain"})).status_code == 415
    big = b"0" * (6 * 1024 * 1024)
    assert (await c.post(f"/v1/public/sessions/{sid}/voice", content=big, headers=auth | {"content-type": "audio/wav"})).status_code == 413
    await c.aclose()


async def test_a_failed_transcription_asks_the_person_to_retry_or_type(setup):
    c, e = await setup(Down())
    sid, auth = await session(c, e)
    r = await c.post(f"/v1/public/sessions/{sid}/voice", content=b"RIFF", headers=auth | {"content-type": "audio/wav"})
    assert r.status_code == 502 and "type" in r.json()["detail"] and "provider" not in r.json()["detail"]
    await c.aclose()


def sign_twilio(url: str, form: dict[str, str]) -> str:
    signed = url + "".join(k + form[k] for k in sorted(form))
    return base64.b64encode(hmac.new(b"twilio-secret", signed.encode(), hashlib.sha1).digest()).decode()


async def twilio(c, path: str, form: dict[str, str], *, sign: bool = True):
    headers = {"x-twilio-signature": sign_twilio(BASE + path, form) if sign else "forged"}
    return await c.post(path, data=form, headers=headers)


async def test_a_phone_call_runs_turn_by_turn_with_signed_webhooks(setup):
    c, e = await setup(FakeTranscriber())
    path = f"/v1/public/phone/embeds/{e['id']}"
    assert (await twilio(c, path, {"From": "+14155550100", "CallSid": "CA1"}, sign=False)).status_code == 403  # not from Twilio
    r = await twilio(c, path, {"From": "+14155550100", "CallSid": "CA1"})
    assert r.status_code == 200 and "<Say>Hi! Ready?</Say>" in r.text and "<Gather" in r.text
    nxt = re.search(r'action="([^"]+)"', r.text).group(1).replace("&amp;", "&")
    r = await twilio(c, nxt, {"SpeechResult": "", "CallSid": "CA1"})
    assert "didn't catch" in r.text and "<Gather" in r.text  # silence: asked again, nothing sent to the agent
    r = await twilio(c, nxt, {"SpeechResult": "hello", "CallSid": "CA1"})
    assert r.status_code == 200 and "<Say>" in r.text and "<Gather" in r.text, r.text
    forged = re.sub(r"t=[^&]+", "t=forged", nxt)
    assert (await twilio(c, forged, {"SpeechResult": "hi", "CallSid": "CA1"})).status_code == 401
    await c.aclose()


@pytest.mark.skipif(not os.getenv("OPENROUTER_API_KEY"), reason="needs OPENROUTER_API_KEY")
async def test_an_audio_chat_model_transcribes_speech():
    from yachat.voice import ChatAudioTranscriber, LiteLLMSpeaker

    if not os.getenv("OPENAI_API_KEY"):
        pytest.skip("needs OPENAI_API_KEY to make the test audio")
    wav = await LiteLLMSpeaker("openai/tts-1", "alloy", api_key=os.environ["OPENAI_API_KEY"]).speak("Seven out of ten.")
    heard = await ChatAudioTranscriber("openrouter/google/gemini-3.8-flash").transcribe(wav, "mp3")
    assert "seven" in heard.lower() or "7" in heard

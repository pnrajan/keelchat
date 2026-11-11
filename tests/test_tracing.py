"""Sentry tracing: off without SENTRY_DSN; when on, spans for stages and model calls, and never what people typed."""

from __future__ import annotations

import json

import pytest

sentry_sdk = pytest.importorskip("sentry_sdk")

from keelchat import tracing  # noqa: E402
from tests.helpers import make_runtime, say  # noqa: E402

SECRET = "my card is 4111 1111 1111 1111 and I live at 12 Elm St"


def test_tracing_is_off_without_a_dsn(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.setattr(tracing, "_on", False)
    assert tracing.init("api") is False
    with tracing.span("x", "y"):  # a no-op, not an error
        pass


def test_request_bodies_cookies_and_local_variables_are_dropped():
    event = {
        "request": {"data": SECRET, "cookies": "c", "query_string": "q=1", "headers": {"Authorization": "Bearer k", "User-Agent": "ua"}},
        "exception": {"values": [{"stacktrace": {"frames": [{"function": "f", "vars": {"text": SECRET}}]}}]},
    }
    out = tracing._scrub(event, {})
    assert out is not None and SECRET not in json.dumps(out)
    assert out["request"]["headers"] == {"User-Agent": "ua"}


async def test_a_turn_is_traced_by_stage_and_model_call_without_its_text(policy_yaml, monkeypatch):
    sent: list[dict] = []

    class Capture(sentry_sdk.transport.Transport):
        def capture_envelope(self, envelope):  # what would leave for Sentry
            for item in envelope.items:
                if item.payload.json:
                    sent.append(item.payload.json)

    monkeypatch.setattr(tracing, "_on", False)
    monkeypatch.setenv("SENTRY_DSN", "https://public@o0.ingest.sentry.io/0")
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")
    real_init = sentry_sdk.init
    monkeypatch.setattr(sentry_sdk, "init", lambda **kw: real_init(transport=Capture, **kw))
    assert tracing.init("test")
    try:
        rt, _ = await make_runtime(policy_yaml)
        s = await rt.start_session("hiring-agent", "u1")
        with sentry_sdk.start_transaction(op="http.server", name="POST /v1/sessions/{id}/turns"):
            await say(rt, s.id, SECRET)
        sentry_sdk.flush()
    finally:
        sentry_sdk.get_client().close()
        monkeypatch.setattr(tracing, "_on", False)
    tx = next(e for e in sent if e.get("type") == "transaction")
    ops = {sp["op"] for sp in tx["spans"]}
    assert {"keelchat.stage", "keelchat.step", "keelchat.model"} <= ops, sorted(ops)
    assert tx["tags"]["agent"] == "hiring-agent" and tx["contexts"]["turn"]["session_id"] == s.id
    assert "4111" not in json.dumps(sent) and "Elm St" not in json.dumps(sent)

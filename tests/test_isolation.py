"""Workspace isolation across surveys, findings, the widget and voice: a workspace's key
never reads, changes or sends another workspace's data, and public tokens never cross workspaces."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from keelchat.api import public
from keelchat.api.app import create_app
from keelchat.policy.compiler import build
from keelchat.state.models import SurveyResponse
from keelchat.state.store import InMemoryEmbedStore, InMemoryTenantToolStore
from tests.helpers import make_runtime

SURVEY = (Path(__file__).parents[1] / "examples" / "nps_survey.yaml").read_text()
ACME, GLOBEX = {"authorization": "Bearer acme-key"}, {"authorization": "Bearer globex-key"}


@pytest.fixture
async def c(monkeypatch):
    monkeypatch.setenv("KEELCHAT_TENANT_KEYS", "acme:acme-key,globex:globex-key")
    monkeypatch.setenv("KEELCHAT_API_KEY", "admin-key")
    monkeypatch.setenv("KEELCHAT_EMBED_SECRET", "s")
    monkeypatch.setattr(public, "_SECRET", None)
    rt, _ = await make_runtime(SURVEY)
    for tenant in ("acme", "globex"):  # the same agent name in both workspaces
        await rt.register_policy(build(SURVEY, tenant=tenant))
        await rt.responses.add(SurveyResponse(tenant=tenant, agent="nps-survey", policy_hash="h", session_id=f"s-{tenant}", user_id="u", answers={"nps": 9}))
    app = create_app(rt, tool_store=InMemoryTenantToolStore(), embeds=InMemoryEmbedStore())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client


async def test_one_workspace_never_reaches_anothers_data(c):
    eid = (await c.post("/v1/tenants/acme/agents/nps-survey/embeds", json={"name": "site"}, headers=ACME)).json()["id"]
    # globex's key, pointed at acme's workspace: refused before anything runs
    for method, path, body in [
        ("GET", "/v1/tenants/acme/embeds", None),
        ("POST", f"/v1/tenants/acme/embeds/{eid}", {"enabled": False}),
        ("GET", "/v1/tenants/acme/agents/nps-survey/findings", None),
        ("GET", "/v1/tenants/acme/agents/nps-survey/responses.csv", None),
        ("GET", "/v1/tenants/acme/metrics", None),
        ("GET", "/v1/tenants/acme/usage/alternatives", None),
    ]:
        r = await c.request(method, path, json=body, headers=GLOBEX)
        assert r.status_code == 403, (path, r.status_code)
    # in its own workspace, globex sees only its own
    assert (await c.post(f"/v1/tenants/globex/embeds/{eid}", json={"enabled": False}, headers=GLOBEX)).status_code == 404
    f = (await c.get("/v1/tenants/globex/agents/nps-survey/findings", headers=GLOBEX)).json()
    assert f["respondents"] == 1  # its own response, not acme's


async def test_public_tokens_never_cross_workspaces(c):
    a = (await c.post("/v1/tenants/acme/agents/nps-survey/embeds", json={"name": "a"}, headers=ACME)).json()["id"]
    g = (await c.post("/v1/tenants/globex/agents/nps-survey/embeds", json={"name": "g"}, headers=GLOBEX)).json()["id"]
    sa = (await c.post(f"/v1/public/embeds/{a}/sessions", json={})).json()
    sg = (await c.post(f"/v1/public/embeds/{g}/sessions", json={})).json()
    # acme's conversation token on globex's conversation, and the other way round
    for sid, tok in ((sg["session_id"], sa["token"]), (sa["session_id"], sg["token"])):
        r = await c.post(f"/v1/public/sessions/{sid}/turns", json={"text": "hi"}, headers={"authorization": f"Bearer {tok}"})
        assert r.status_code == 401

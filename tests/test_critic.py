"""A stronger model judges a trial conversation and proposes fixes: policy fixes go to the drafter, code fixes are
kept as fix tasks that Claude Code picks up over MCP (and may file as GitHub issues). The model judges; code checks
the turns it names and caps the counts."""

from __future__ import annotations

from dataclasses import replace

from httpx import ASGITransport, AsyncClient

from tests.conftest import ROOT
from tests.helpers import make_runtime
from yachat.evals.critic import FIXES_LIMIT, _checked, issue_markdown, policy_request
from yachat.runtime.llm import ModelConfig
from yachat.state.models import FixTask, ReviewTurn

SUPPORT = (ROOT / "examples" / "support_assistant.yaml").read_text()
PATH = "/v1/tenants/default/agents/support-assistant"
#: the agent ignores the damaged-item rule (offer a replacement) and promises a refund it cannot give
BAD = [
    {"user": "hi", "agent": "Hi! I can help with orders, returns and shipping. Is it OK if I note your details?"},
    {"user": "yes", "agent": "Thanks. What is your order number?"},
    {"user": "ORD-12345, my blender arrived broken", "agent": "Sorry! I've refunded you $500 and you'll get it in an hour.", "rules": ["damaged_item"]},
]
TASK = {
    "title": "Hold refund promises to the tool's result",
    "problem": "Promised a refund",
    "change": "Check the gateway result before promising",
    "test": "a refund is never promised without a refund tool call",
    "files": ["yachat/runtime/respond/rewriter.py"],
    "turns": BAD,
}


async def _client(critic: str = ""):  # type: ignore[no-untyped-def]
    from yachat.api.app import create_app
    from yachat.state.store import InMemoryTenantToolStore

    env = ModelConfig.from_env()
    rt, _ = await make_runtime(SUPPORT, models=replace(env, critic=critic and env.generate))
    return rt, AsyncClient(transport=ASGITransport(app=create_app(rt, tool_store=InMemoryTenantToolStore())), base_url="http://t", timeout=300)


def test_only_real_turns_are_kept_and_counts_are_capped() -> None:
    raw = {
        "summary": "went badly",
        "findings": [
            {"turn": 9, "severity": "high", "problem": "no such turn"},
            {"turn": 2, "severity": "low", "problem": "slow"},
            {"turn": 3, "severity": "high", "problem": "false promise"},
        ],
        "fixes": [
            {"kind": "policy", "title": "Add a rule", "change": "x", "turns": [3, 7], "files": ["a.py"], "test": "t"},
            {"kind": "code", "title": "Nowhere", "change": "x", "turns": [0], "files": [], "test": ""},
        ]
        + [{"kind": "code", "title": f"c{i}", "change": "x", "turns": [1], "files": ["b.py"], "test": "t"} for i in range(9)],
    }
    c = _checked("m", raw, n=3)
    assert [f.turn for f in c.findings] == [3, 2]  # the made-up turn is gone, the worst comes first
    assert c.fixes[0].turns == [3] and c.fixes[0].files == [] and c.fixes[0].test == ""  # a policy fix carries no code hints
    assert "Nowhere" not in [f.title for f in c.fixes] and len(c.fixes) == FIXES_LIMIT
    assert policy_request(c.fixes[:1]) == "- Add a rule: x"


def test_a_fix_task_reads_as_an_issue() -> None:
    md = issue_markdown(FixTask(tenant="t", agent="support-assistant", **{k: v for k, v in TASK.items() if k != "turns"}, turns=[ReviewTurn(**t) for t in BAD]))
    parts = ("## Problem", "## Change", "## Test (fails before the fix)", "`yachat/runtime/respond/rewriter.py`", "> **User:** hi", "fix task `fix_")
    for part in parts:
        assert part in md, part


async def test_fix_tasks_are_kept_listed_closed_and_linked() -> None:
    _, client = await _client()
    async with client:
        saved = (await client.post(f"{PATH}/fixes", json=TASK)).json()
        assert saved["id"].startswith("fix_") and saved["status"] == "open"
        assert [f["id"] for f in (await client.get("/v1/tenants/default/fixes?status=open")).json()] == [saved["id"]]
        one = (await client.get(f"/v1/tenants/default/fixes/{saved['id']}")).json()
        assert "## Change" in one["issue_markdown"]
        url = "https://github.com/acme/yachat/issues/7"
        done = (await client.patch(f"/v1/tenants/default/fixes/{saved['id']}", json={"status": "done", "note": "PR #8", "issue": url})).json()
        assert done["status"] == "done" and done["issue"] == url and done["title"] == TASK["title"]
        assert (await client.get("/v1/tenants/default/fixes?status=open")).json() == []
        assert (await client.patch(f"/v1/tenants/default/fixes/{saved['id']}", json={"issue": "javascript:alert(1)"})).status_code == 422
        assert (await client.get("/v1/tenants/default/fixes/fix_000000000000")).status_code == 404
        assert (await client.post("/v1/tenants/default/agents/nobody/fixes", json=TASK)).status_code == 404


async def test_without_a_critic_model_judging_is_refused() -> None:
    _, client = await _client()
    async with client:
        assert (await client.get("/v1/critic")).json() == {"model": None}
        r = await client.post(f"{PATH}/critique", json={"turns": BAD, "yaml": SUPPORT})
        assert r.status_code == 422 and "YACHAT_MODEL_CRITIC" in r.text


async def test_the_critic_names_the_broken_turn_and_proposes_fixes() -> None:
    _, client = await _client(critic="on")
    async with client:
        r = await client.post(f"{PATH}/critique", json={"turns": BAD, "yaml": SUPPORT})
        assert r.status_code == 200, r.text
        c = r.json()
    assert c["model"] and any(f["turn"] == 3 for f in c["findings"]), c
    assert c["fixes"] and all(f["turns"] and set(f["turns"]) <= {1, 2, 3} for f in c["fixes"]), c

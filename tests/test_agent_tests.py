"""Tests kept with the agent (the console's Test tab): saved, run on the current build, drafted from the policy, exported
as a replay set; and policy changes suggested from people's ratings."""

from __future__ import annotations

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from keelchat.evals.generate import case_name, targets
from keelchat.evals.replay import ReplaySet
from keelchat.evals.review import feedback
from keelchat.policy.compiler import build
from keelchat.state.models import ReplyRating, Review, ReviewTurn
from tests.conftest import ROOT
from tests.helpers import make_runtime

SUPPORT = (ROOT / "examples" / "support_assistant.yaml").read_text()
PATH = "/v1/tenants/default/agents/support-assistant"
DONE = {"name": "finishes", "messages": ["hi, I need help with an order", "yes", "ORD-12345", "it never arrived"], "expect": {"completed": True}}


async def _client(policy: str = SUPPORT):  # type: ignore[no-untyped-def]
    from keelchat.api.app import create_app
    from keelchat.state.store import InMemoryTenantToolStore

    rt, _ = await make_runtime(policy)
    return rt, AsyncClient(transport=ASGITransport(app=create_app(rt, tool_store=InMemoryTenantToolStore())), base_url="http://t")


async def test_a_test_is_saved_renamed_exported_and_deleted() -> None:
    _, client = await _client()
    async with client:
        saved = (await client.post(f"{PATH}/tests", json={"source": "chat", "case": DONE})).json()
        assert saved["id"].startswith("tst_") and saved["case"]["expect"]["completed"] is True
        other = (await client.post(f"{PATH}/tests", json={"case": DONE | {"name": "second"}})).json()
        # names are unique per agent: the set exports as one replay.yaml
        assert (await client.post(f"{PATH}/tests", json={"case": DONE})).status_code == 409
        assert (await client.post(f"{PATH}/tests", json={"id": other["id"], "case": DONE})).status_code == 409
        changed = (await client.post(f"{PATH}/tests", json={"id": saved["id"], "case": DONE | {"name": "renamed"}})).json()
        assert changed["created_at"] == saved["created_at"]
        assert [t["case"]["name"] for t in (await client.get(f"{PATH}/tests")).json()] == ["renamed", "second"]  # oldest first
        text = (await client.get(f"{PATH}/tests.yaml")).text
        assert (await client.delete(f"{PATH}/tests/{other['id']}")).status_code == 204
        assert (await client.delete(f"{PATH}/tests/{other['id']}")).status_code == 404
        assert [t["case"]["name"] for t in (await client.get(f"{PATH}/tests")).json()] == ["renamed"]
    cases = ReplaySet.model_validate(yaml.safe_load(text)).cases  # loads as `keelchat evals` reads it
    assert [c.name for c in cases] == ["renamed", "second"] and cases[0].expect.completed is True


async def test_a_bad_test_is_rejected_and_tests_stay_with_their_agent() -> None:
    rt, client = await _client()
    await rt.register_policy(build(SUPPORT.replace("agent: support-assistant", "agent: other-bot")))
    async with client:
        for bad, where in [
            ({"name": "has space", "messages": ["hi"]}, "case.name"),
            ({"name": "x", "messages": ["hi"], "channel": "fax"}, "case.channel"),
            ({"name": "x"}, "messages or persona"),
            ({"name": "x", "messages": ["hi"], "expect": {"mood": "happy"}}, "mood"),
        ]:
            r = await client.post(f"{PATH}/tests", json={"case": bad})
            assert r.status_code == 422 and where in r.text, (bad, r.text)
        assert (await client.post("/v1/tenants/default/agents/nobody/tests", json={"case": DONE})).status_code == 404
        mine = (await client.post(f"{PATH}/tests", json={"case": DONE})).json()
        theirs = "/v1/tenants/default/agents/other-bot/tests"
        assert (await client.post(theirs, json={"id": mine["id"], "case": DONE})).status_code == 404
        assert (await client.delete(f"{theirs}/{mine['id']}")).status_code == 404
        assert (await client.post(f"{PATH}/tests/run", json={"ids": ["tst_000000000000"]})).status_code == 404


async def test_saved_tests_run_on_the_current_build(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EVALS_JUDGE_MODEL", "")  # code checks only (the app reads .env, which may name a judge)
    _, client = await _client()
    async with client:
        ok = (await client.post(f"{PATH}/tests", json={"case": DONE})).json()
        wrong = (await client.post(f"{PATH}/tests", json={"case": DONE | {"name": "wrong", "expect": {"exit_reason": "handed_off"}}})).json()
        run = (await client.post(f"{PATH}/tests/run", json={})).json()
    by = {r["id"]: r for r in run["results"]}
    assert run["judge"] is None and run["policy_hash"]
    assert by[ok["id"]]["passed"], by[ok["id"]]["problems"]
    assert by[ok["id"]]["outcome"]["fields"]["order_number"] and len(by[ok["id"]]["turns"]) == 4
    assert not by[wrong["id"]]["passed"] and any("exit_reason" in p for p in by[wrong["id"]]["problems"])


async def test_drafting_skips_targets_that_already_have_a_test() -> None:
    _, client = await _client()
    async with client:
        for t in targets(build(SUPPORT)):  # every target already drafted: nothing to play, no model call
            await client.post(f"{PATH}/tests", json={"source": "draft", "case": {"name": case_name(t.id), "messages": ["hi"]}})
        r = (await client.post(f"{PATH}/tests/draft", json={})).json()
    assert r == {"cases": [], "failures": {}, "not_reached": {}, "targets": []}


async def test_readiness_counts_tests_kept_with_the_agent() -> None:
    policy = SUPPORT.replace("agent: support-assistant", "agent: tested-bot")  # no eval set on disk
    _, client = await _client(policy)
    path = "/v1/tenants/default/agents/readiness"
    async with client:
        before = {i["id"]: i for i in (await client.post(path, json={"yaml": policy})).json()["items"]}
        await client.post("/v1/tenants/default/agents/tested-bot/tests", json={"case": DONE})
        after = {i["id"]: i for i in (await client.post(path, json={"yaml": policy})).json()["items"]}
    assert before["tests"]["status"] == "missing" and before["tests"]["fix"] == "tests" and after["tests"]["status"] == "ok"


def test_feedback_lists_what_people_failed_newest_rating_first() -> None:
    turns = [ReviewTurn(user="hi", agent="Hello!"), ReviewTurn(user="my blender arrived broken", agent="Sorry.", rules=["damaged_item"])]
    failed = ReplyRating(turn=2, check="rule_followed:damaged_item", ok=False, note="no replacement offered")
    old = Review(tenant="t", agent="a", source="chat", conversation="c1", turns=turns, replies=[failed])
    fixed = old.model_copy(update={"id": "rev_bbbbbbbbbbbb", "replies": [failed.model_copy(update={"ok": True})]})
    low = Review(tenant="t", agent="a", source="chat", conversation="c2", turns=turns, score=1, note="pushy")
    quiet = Review(tenant="t", agent="a", source="chat", conversation="c3", turns=turns, score=2)  # no note: nothing to act on
    assert feedback([quiet, low, old]) == [
        "A conversation scored 1/5; the rater said: pushy",
        'Not followed rule damaged_item. User: "my blender arrived broken" Agent: "Sorry."; the rater said: no replacement offered',
    ]
    assert feedback([fixed, old]) == []  # rated again and passed: only the newest rating counts


async def test_suggested_changes_answer_the_ratings_and_build() -> None:
    _, client = await _client()
    review = {
        "source": "chat",
        "conversation": "ses_1",
        "turns": [
            {"user": "hi", "agent": "Hi! May I note details from this chat?", "rules": []},
            {"user": "can you help me sue the courier?", "agent": "Sure, here is how to file a lawsuit: ...", "rules": []},
        ],
        "replies": [{"turn": 2, "check": "in_scope", "ok": False, "note": "gave legal advice instead of declining"}],
    }
    async with client:
        assert (await client.post(f"{PATH}/reviews/suggest", json={"yaml": SUPPORT})).status_code == 422  # nothing rated yet
        await client.post(f"{PATH}/reviews", json=review)
        r = (await client.post(f"{PATH}/reviews/suggest", json={"yaml": SUPPORT})).json()
    assert r["feedback"] and "legal advice" in r["feedback"][0]
    assert r["errors"] == [], r["errors"]
    assert build(r["yaml"]).agent == "support-assistant"  # the same agent, and it builds


def test_the_yaml_is_taken_from_a_reply_that_says_something_first() -> None:
    from keelchat.policy.drafter import _yaml_only

    assert _yaml_only("Here is the revised policy:\n\n# change: x\nagent: a\n") == "# change: x\nagent: a\n"
    assert _yaml_only("```yaml\nagent: a\n```\nDone.") == "agent: a\n"

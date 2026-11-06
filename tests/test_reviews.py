"""People rating conversations (the console's Review tab): stored per agent, exported as judge labels and replay cases."""

from __future__ import annotations

import yaml
from httpx import ASGITransport, AsyncClient

from tests.conftest import ROOT
from tests.helpers import make_runtime
from yachat.evals.calibrate import LabelSet
from yachat.evals.replay import ReplaySet
from yachat.state.models import Review, ReviewTurn

SUPPORT = (ROOT / "examples" / "support_assistant.yaml").read_text()
TURNS = [
    {"user": "hi", "agent": "Hi! May I note details from this chat?", "rules": []},
    {"user": "yes", "agent": "Thanks. What is your order number?", "rules": []},
    {"user": "my blender arrived broken", "agent": "Thanks.", "rules": ["damaged_item"]},
]


async def _client():  # type: ignore[no-untyped-def]
    from yachat.api.app import create_app
    from yachat.state.store import InMemoryTenantToolStore

    rt, _ = await make_runtime(SUPPORT)
    return rt, AsyncClient(transport=ASGITransport(app=create_app(rt, tool_store=InMemoryTenantToolStore())), base_url="http://t")


async def test_a_rating_is_saved_listed_changed_and_exported() -> None:
    rt, client = await _client()
    path = "/v1/tenants/default/agents/support-assistant/reviews"
    body = {
        "source": "chat",
        "conversation": "ses_abc",
        "turns": TURNS,
        "replies": [
            {"turn": 1, "check": "in_scope", "ok": True},
            {"turn": 3, "check": "rule_followed:damaged_item", "ok": False, "note": "no replacement offered"},
        ],
        "score": 2,
        "note": "ignored the broken blender",
        "rater": "ana",
    }
    async with client:
        saved = (await client.post(path, json=body)).json()
        assert saved["id"].startswith("rev_") and saved["agent"] == "support-assistant"
        changed = (await client.post(path, json=body | {"id": saved["id"], "score": 1})).json()
        assert changed["id"] == saved["id"] and changed["score"] == 1
        listed = (await client.get(path)).json()
        assert [r["id"] for r in listed] == [saved["id"]]  # replaced, not added
        labels = (await client.get(f"{path}/labels.yaml")).text
        cases = (await client.get(f"{path}/replay.yaml")).text
    got = LabelSet.model_validate(yaml.safe_load(labels)).labels  # loads as calibrate reads it
    assert [(lb.check, len(lb.turns), lb.passes) for lb in got] == [("in_scope", 1, True), ("rule_followed:damaged_item", 3, False)]
    assert got[1].note == "no replacement offered; chat ses_abc"
    (case,) = ReplaySet.model_validate(yaml.safe_load(cases)).cases  # loads as a replay set
    assert case.messages == ["hi", "yes", "my blender arrived broken"] and case.name.startswith("review_ses_abc_")
    assert "rule_followed:damaged_item on turn 3 (no replacement offered)" in case.note and "scored 1/5" in case.note
    assert await rt.reviews.list("other", "support-assistant") == []
    assert changed["created_at"] == saved["created_at"]  # a changed rating keeps its place


async def test_a_rating_names_an_agent_that_exists_and_its_own_id() -> None:
    from yachat.policy.compiler import build

    rt, client = await _client()
    await rt.register_policy(build(SUPPORT.replace("agent: support-assistant", "agent: other-bot")))
    body = {"source": "chat", "conversation": "c", "turns": TURNS, "score": 3}
    async with client:
        assert (await client.post("/v1/tenants/default/agents/nobody/reviews", json=body)).status_code == 404
        assert (await client.get("/v1/tenants/default/agents/nobody/reviews/labels.yaml")).status_code == 404
        mine = (await client.post("/v1/tenants/default/agents/support-assistant/reviews", json=body)).json()
        # another agent's ratings are not reachable by id: no overwrite, no move
        other = await client.post("/v1/tenants/default/agents/other-bot/reviews", json=body | {"id": mine["id"], "score": 1})
        assert other.status_code == 404
        unknown = await client.post("/v1/tenants/default/agents/support-assistant/reviews", json=body | {"id": "rev_000000000000"})
        assert unknown.status_code == 404
    (kept,) = await rt.reviews.list("default", "support-assistant")
    assert kept.score == 3


async def test_a_bad_rating_is_rejected_with_where() -> None:
    _, client = await _client()
    path = "/v1/tenants/default/agents/support-assistant/reviews"
    base = {"source": "simulate", "conversation": "cooperative", "turns": TURNS}
    async with client:
        for bad, where in [
            ({"replies": [{"turn": 4, "check": "in_scope", "ok": True}]}, "replies[0].turn"),
            ({"replies": [{"turn": 1, "check": "tone", "ok": True}]}, "check"),
            ({"replies": [{"turn": 1, "check": "in_scope", "ok": "no"}]}, "ok"),
            ({"score": 6}, "score"),
            ({"replies": [{"turn": 1, "check": "in_scope", "ok": True}, {"turn": 1, "check": "in_scope", "ok": False}]}, "rated twice"),
            ({"replies": [{"turn": 1, "check": "rule_followed:damaged_item", "ok": True}]}, "did not match on turn 1"),
            ({"turns": []}, "turns"),
        ]:
            r = await client.post(path, json=base | bad)
            assert r.status_code == 422 and where in r.text, (bad, r.text)


def test_only_failed_or_low_scored_conversations_become_cases() -> None:
    from yachat.evals.review import cases_yaml, labels_yaml

    turns = [ReviewTurn.model_validate(t) for t in TURNS]
    good = Review(tenant="t", agent="a", source="chat", conversation="ok", turns=turns, score=5)
    low = Review(tenant="t", agent="a", source="chat", conversation="low", turns=turns, score=2, note="pushy")
    empty_reply = Review(tenant="t", agent="a", source="chat", conversation="e", turns=[ReviewTurn(user="hi", agent="")], replies=[])
    (case,) = ReplaySet.model_validate(yaml.safe_load(cases_yaml([good, low, empty_reply]))).cases
    assert case.messages == ["hi", "yes", "my blender arrived broken"] and case.note == "a person failed it: scored 2/5 (pushy)"
    assert yaml.safe_load(labels_yaml([good, low]))["labels"] == []  # scores alone are not labels
    # rated again after it changed: only the newest rating of a conversation counts
    rated = [{"turn": 1, "check": "in_scope", "ok": False}]
    first = Review(tenant="t", agent="a", source="chat", conversation="ok", turns=turns, replies=rated)
    again = first.model_copy(update={"id": "rev_bbbbbbbbbbbb", "replies": [r.model_copy(update={"ok": True}) for r in first.replies]})
    assert [lb["pass"] for lb in yaml.safe_load(labels_yaml([first, again]))["labels"]] == [True]
    assert yaml.safe_load(cases_yaml([first, again]))["cases"] == []
    blank = Review(tenant="t", agent="a", source="chat", conversation="b", turns=[ReviewTurn(user="hi", agent="")], replies=rated)
    assert labels_yaml([blank]).startswith("# yachat review: replies a person rated (yachat evals calibrate) (1 left out")

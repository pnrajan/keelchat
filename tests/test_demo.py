"""The public demo (examples/demo): what a drafted bot needs connecting, the landing page YAML, trying a draft."""

from __future__ import annotations

import pytest

from examples.demo.needs import from_policy
from keelchat.policy.compiler import build
from tests.conftest import ROOT


def test_the_list_names_what_the_yaml_uses_and_how_to_connect_it() -> None:
    needs = {n.name: n for n in from_policy(build((ROOT / "examples" / "support_assistant.yaml").read_text()))}
    # the support example searches documents, hands over to people and remembers a contact preference
    assert {"A model key", "Your documents", "Somewhere for handed-over chats", "A database for what it remembers", "Where people talk to it"} <= set(needs)
    assert "preferred_contact" in needs["A database for what it remembers"].why
    assert all(n.how and n.docs.startswith("https://") and n.options and n.source == "yaml" for n in needs.values())


def test_a_bot_without_documents_or_handoff_is_not_asked_for_them() -> None:
    needs = {n.name for n in from_policy(build("agent: greeter\ncharter: 'Say hello. Out of scope: everything else.'\nflow: [greet_consent]\n"))}
    assert "Your documents" not in needs and "Somewhere for handed-over chats" not in needs
    assert "A model key" in needs


def test_the_landing_page_yaml_builds() -> None:
    """site/index.html shows a bot; it must be one the build accepts."""
    import html
    import re

    page = (ROOT / "site" / "index.html").read_text()
    shown = re.search(r"<pre[^>]*><span class=\"k\">agent:.*?</pre>", page, re.S)
    assert shown is not None
    assert build(html.unescape(re.sub(r"<[^>]+>", "", shown.group(0)))).agent == "support-assistant"


def test_a_draft_can_be_put_up_to_chat_with_and_a_broken_one_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    for k in ("KNOWLEDGE_S3_BUCKET", "KNOWLEDGE_INDEX_URI"):  # the demo's knowledge index stays in memory in tests
        monkeypatch.delenv(k, raising=False)

    from examples.demo.app import create_app

    yaml = (ROOT / "examples" / "support_assistant.yaml").read_text()
    with TestClient(create_app()) as c:
        first, second = c.post("/demo/try", json={"yaml": yaml}), c.post("/demo/try", json={"yaml": yaml})
        assert first.status_code == second.status_code == 200
        assert first.json()["agent"] == "support-assistant"
        assert first.json()["embed"] != second.json()["embed"]  # each visitor gets their own bot
        session = c.post(f"/v1/public/embeds/{first.json()['embed']}/sessions", json={})
        assert session.status_code == 201 and session.json()["token"]

        broken = c.post("/demo/try", json={"yaml": "agent: x\nflow: [no_such_step]\n"})
        assert broken.status_code == 422 and "doesn't build" in broken.json()["detail"]


# --- questions to click before the first draft -------------------------------------------------------------------


def test_chosen_options_are_added_to_the_description_in_question_order() -> None:
    from keelchat.policy.drafter import Option, Question, with_answers

    qs = [
        Question(
            "emergencies",
            "What about emergencies?",
            "",
            False,
            [Option("Hand to a person", "Emergencies go to a person."), Option("Give a number", "Give the clinic number.")],
        ),
        Question("collect", "What to collect?", "", True, [Option("Phone", "Ask for a phone number."), Option("Email", "Ask for an email.")]),
    ]
    out = with_answers("Book dental appointments.", {"collect": ["Email", "Phone"], "emergencies": ["Give a number", "Hand to a person"], "nope": ["x"]}, qs)
    # one pick on a single-choice question
    assert out == "Book dental appointments.\n\nDetails:\n- Give the clinic number.\n- Ask for an email.\n- Ask for a phone number."
    assert with_answers("Book.", {"collect": ["Only on weekdays"]}, qs) == "Book.\n\nDetails:\n- What to collect? Only on weekdays"  # their own words
    assert with_answers("Book.", {}, qs) == "Book."


async def test_malformed_questions_from_the_model_are_dropped() -> None:
    from typing import Any

    from keelchat.policy.drafter import CLARIFY_MAX, clarify
    from keelchat.runtime.llm import LLMClient, LLMRequest, ModelConfig

    class Canned(LLMClient):
        async def complete(self, req: LLMRequest) -> str:
            raise AssertionError("not used")

        async def structured(self, req: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
            ok = {"ask": "Which languages?", "options": [{"label": "English", "adds": "English only."}, {"label": "Both", "adds": "English and Spanish."}]}
            return {
                "questions": [
                    {"id": "Languages!", **ok},
                    {"id": "languages", **ok},  # duplicate id
                    {"id": "one", "ask": "One option?", "options": [{"label": "Only", "adds": "x"}]},  # too few options
                    {"id": "blank", "ask": "", "options": ok["options"]},  # no question
                    "not a question",
                    *({"id": f"q{i}", **ok} for i in range(10)),
                ]
            }

    qs = (await clarify(Canned(), ModelConfig(), "Answer questions about our shop.")).questions
    assert qs[0].id == "languages" and [o.label for o in qs[0].options] == ["English", "Both"]
    assert len(qs) == CLARIFY_MAX and len({q.id for q in qs}) == CLARIFY_MAX


async def test_the_drafting_model_asks_what_the_description_leaves_open() -> None:
    from keelchat.policy.drafter import clarify
    from keelchat.runtime.llm import ModelConfig
    from keelchat.runtime.llm_factory import llm_from_env

    c = await clarify(llm_from_env(), ModelConfig.from_env(), "Book dental appointments for our clinic.")
    assert c.understood and 2 <= len(c.questions) <= 5 and all(2 <= len(q.options) <= 5 and q.ask for q in c.questions)


def test_the_demo_asks_first_and_drafts_with_the_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml as pyyaml
    from fastapi.testclient import TestClient

    from examples.demo.app import create_app

    for k in ("KNOWLEDGE_S3_BUCKET", "KNOWLEDGE_INDEX_URI"):
        monkeypatch.delenv(k, raising=False)
    text = "Answer questions about our bike shop."
    with TestClient(create_app()) as c:
        r = c.post("/demo/clarify", json={"text": text})
        assert r.status_code == 200 and isinstance(r.json()["questions"], list) and "understood" in r.json()
        lang = {"id": "languages", "ask": "Which languages?", "multi": False, "options": [{"label": "Both", "adds": "The bot speaks English and Spanish."}]}
        d = c.post("/demo/draft", json={"text": text, "questions": [lang], "answers": {"languages": ["Both"]}}).json()
    assert not d["errors"], d["errors"]
    assert "es" in pyyaml.safe_load(d["yaml"]).get("languages", [])  # the picked option reached the draft

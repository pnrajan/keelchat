"""Conversation surveys: objectives found out in a natural conversation, with the real model. One reply can cover
several objectives, the score is read in code, kept words are the person's own, and results read it like any survey."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers import make_runtime, say
from yachat.capabilities.nodes.conversation_survey import ConversationSurveyConfig, Objective
from yachat.capabilities.nodes.survey_answers import QuestionKind, survey_questions_of
from yachat.policy.compiler import build

FEEDBACK = (Path(__file__).parents[1] / "examples" / "customer_feedback.yaml").read_text()


def test_objectives_become_survey_questions_for_results():
    qs = {q.key: q for q in survey_questions_of(build(FEEDBACK).steps)}
    assert qs["nps"].kind is QuestionKind.scale and (qs["nps"].scale_min, qs["nps"].scale_max) == (0, 10)
    assert qs["experience"].kind is QuestionKind.open and "stood out" in qs["experience"].expectation
    with pytest.raises(ValueError, match="two options"):
        Objective(key="pick", learn="Which they prefer", measure="choice", options=["only"])
    with pytest.raises(ValueError, match="share a key"):
        ConversationSurveyConfig(objectives=[Objective(key="a", learn="one thing"), Objective(key="a", learn="another")])


async def test_one_reply_can_answer_several_objectives_and_the_survey_adapts():
    rt, _ = await make_runtime(FEEDBACK)
    s = await rt.start_session("customer-feedback", "u1")
    await say(rt, s.id, "hi")
    r = await say(rt, s.id, "yes")
    assert "recommend" in r.outbound.text.lower(), r.outbound.text
    reply = "I'd say 8. The sourdough was amazing but delivery took over a week, which was annoying."
    r = await say(rt, s.id, reply)
    fields = r.state.fields
    assert fields["nps"] == 8  # read in code
    # the same reply spoke about their experience: it is kept in their own words, and the survey doesn't ask again
    kept = str(fields.get("experience") or r.state.data["conversation_survey"]["drafts"].get("experience") or "")
    assert kept and kept.lower() in reply.lower(), kept
    for _ in range(4):
        if r.state.completed:
            break
        r = await say(rt, s.id, "Faster delivery, honestly. Maybe offer a tracking link.")
    assert r.state.completed, r.outbound.text
    await rt.drain()
    saved = await rt.responses.list("default", "customer-feedback")
    assert saved[-1].complete and saved[-1].answers["nps"] == 8 and "delivery" in str(saved[-1].answers["improve"]).lower()


async def test_nothing_to_improve_is_an_answer_not_a_reason_to_ask_again():
    rt, _ = await make_runtime(FEEDBACK)
    s = await rt.start_session("customer-feedback", "u2")
    for text in ("hi", "sure", "9, the bread was fresh and the packaging was lovely"):
        r = await say(rt, s.id, text)
    for _ in range(2):  # the experience may take one follow-up; then the last objective is asked
        if r.state.data["conversation_survey"].get("current") == "improve":
            break
        r = await say(rt, s.id, "Delivery was on time too.")
    assert r.state.data["conversation_survey"].get("current") == "improve", r.outbound.text
    r = await say(rt, s.id, "nothing really")
    assert r.state.completed, r.outbound.text  # no follow-up pushing for a complaint they don't have
    assert r.outbound.text.lower().count("thank") <= 1, r.outbound.text  # the goodbye thanks them once


async def test_pretend_customers_test_the_survey_and_give_a_confidence_score():
    """One round with the real model: four pretend customers take the survey in a sandbox, and the score is counted
    from what was actually kept (finished, each objective answered), so it can be trusted to compare versions."""
    import asyncio

    from httpx import ASGITransport, AsyncClient

    from yachat.api.app import create_app
    from yachat.state.store import InMemoryTenantToolStore

    rt, _ = await make_runtime(FEEDBACK)
    from yachat.state.store import InMemoryJobStore

    app = create_app(rt, tool_store=InMemoryTenantToolStore(), jobs=InMemoryJobStore())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", timeout=60) as c:
        assert (await c.post("/v1/tenants/default/agents/refine", json={"yaml": "agent: x\nflow: [nope]\n"})).status_code == 422
        job = (await c.post("/v1/tenants/default/agents/refine", json={"yaml": FEEDBACK, "rounds": 1})).json()
        for _ in range(900):
            got = (await c.get(f"/v1/tenants/default/agents/refine/{job['id']}")).json()
            if got["status"] != "running":
                break
            await asyncio.sleep(1)
    assert got["status"] == "done", got
    r = got["result"]["rounds"][0]
    assert 0 <= r["score"] <= 100 and set(r["answered"]) == {"nps", "experience", "improve"}
    assert {c["persona"] for c in r["conversations"]} == {"happy", "unhappy", "chatty", "busy"}
    assert all(c["transcript"] for c in r["conversations"]) and got["result"]["best"] == 0
    assert await rt.responses.list("default", "customer-feedback") == []  # the pretend answers stay in the sandbox


def test_two_sentences_in_a_free_text_answer_are_one_answer_not_a_conflict():
    """'Which one is right?' is for a field that holds one value (an address); a survey's free-text answer given in two
    sentences is joined instead."""
    from yachat.runtime.decide.intent_detector import IntentItem, join_free_text, summarize
    from yachat.state.models import OpenQuestion

    items = [
        IntentItem(intent="new_answer", text="get your shipping act together", fields={"improve": "get your shipping act together"}),
        IntentItem(intent="new_answer", text="don't promise dates you can't keep", fields={"improve": "don't promise dates you can't keep"}),
    ]
    v = summarize(items, "get your shipping act together. don't promise dates you can't keep")
    assert v.conflict is not None  # a one-value field still asks
    open_q = OpenQuestion(step="conversation_survey", field="improve", ask="What should we fix?", expect="", free_text=True)
    joined = join_free_text(v, open_q)
    assert joined.conflict is None and joined.fields["improve"] == "get your shipping act together don't promise dates you can't keep"
    fresh = summarize(items, "")
    assert join_free_text(fresh, open_q.model_copy(update={"free_text": False})).conflict is not None  # a one-value field


async def test_a_clicked_score_starts_the_survey_even_when_it_remembers_things():
    """Clicking a score in the email is choosing to answer, so the consent step is done and the next reply is a survey
    answer, not a yes or no. Remembering still follows the privacy rules: with no explicit yes, consent stays unset, so
    sensitive items are withheld by the memory writer; ordinary items need no yes."""
    remembering = FEEDBACK + (
        "memory:\n"
        "  - field: last_order_feeling\n    extract: how they felt about their last order\n    half_life: 30d\n"
        "  - field: phone_number\n    extract: their phone number\n    sensitive: true\n"
    )
    rt, _ = await make_runtime(remembering)
    s = await rt.start_session("customer-feedback", "u3")
    st = await rt.prefill_answer(s.id, "nps", 6)
    assert st.steps["greet_consent"].status == "complete" and st.fields["nps"] == 6
    assert st.consent is not True  # no explicit yes: sensitive memory stays withheld
    r = await say(rt, s.id, "The beans were great but delivery took 9 days and the bag was split")
    assert r.state.open_question is None or r.state.open_question.step == "conversation_survey", r.outbound.text
    assert "yes or" not in r.outbound.text.lower() and "may i use your answers" not in r.outbound.text.lower(), r.outbound.text

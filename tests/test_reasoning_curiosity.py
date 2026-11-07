"""Replies planned before they are written (`think`), answers from documents only when the documents answer, and one
follow-up on a thin form answer (`curious`): the model judges, code decides and counts."""

from __future__ import annotations

from tests.conftest import ROOT
from tests.helpers import make_runtime, mutate, say
from yachat.evals.generate import targets
from yachat.policy.compiler import build
from yachat.testing.fakes import FakeTools

SUPPORT = (ROOT / "examples" / "support_assistant.yaml").read_text()


def _plain(policy: str) -> str:
    """The support example with neither feature, to compare against."""

    def off(d: dict) -> None:  # type: ignore[type-arg]
        step = d["flow"][1]["form_fill"]
        step.pop("think"), step.pop("curious")

    return mutate(policy, off)


def test_the_options_compile_and_the_old_hash_stays() -> None:
    on, off = build(SUPPORT), build(_plain(SUPPORT))
    assert on.steps[1].think and on.steps[1].settings["curious"] is True
    assert not off.steps[1].think and off.steps[1].settings["curious"] is False
    # a policy that does not use `think` keeps the hash it had before the option existed
    explicit = mutate(_plain(SUPPORT), lambda d: d["flow"][1]["form_fill"].update(think=False))
    assert off.hash == build(explicit).hash
    assert "probe:issue" in [t.id for t in targets(on)] and not [t for t in targets(off) if t.id.startswith("probe:")]


async def test_a_planned_reply_follows_the_rule_that_matched() -> None:
    rt, _ = await make_runtime(SUPPORT)
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    r = await say(rt, s.id, "my blender arrived broken")
    (thought,) = [x for x in r.outbound.items if x["type"] == "reasoning"]
    assert r.rules == ["damaged_item"] and thought["rules"] == ["damaged_item"] and thought["plan"] and thought["need"]
    assert "replace" in r.outbound.text.lower(), r.outbound.text  # what the rule's `then` asks for
    assert "?" in r.outbound.text  # and the open question (the order number) is still asked


async def test_documents_that_do_not_answer_are_not_described() -> None:
    benefits = [{"title": "Benefits", "text": "Health, dental and a 401k match from day one.", "source": "hr.md"}]
    rt, _ = await make_runtime(_plain(SUPPORT), tools=FakeTools(passages=benefits))
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    r = await say(rt, s.id, "do you ship to Canada?")
    text = r.outbound.text.lower()
    assert not any(w in text for w in ("401k", "dental", "passage", "provided text")), r.outbound.text


async def test_a_thin_answer_gets_one_follow_up_and_an_id_gets_none() -> None:
    rt, _ = await make_runtime(SUPPORT)
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    r = await say(rt, s.id, "ORD-12345")
    assert "probes" not in r.state.data  # an order number is never followed up
    r = await say(rt, s.id, "it's broken")
    assert r.state.data["probes"] == {"issue": 1} and r.state.open_question and r.state.open_question.field == "issue__more"
    assert r.state.fields["issue"] and "?" in r.outbound.text  # the thin answer is kept, and one question asks for more
    r = await say(rt, s.id, "the glass jug is cracked down the side")
    assert r.state.completed and "cracked" in r.state.fields["issue"], r.state.fields


async def test_an_answer_given_ahead_of_its_question_is_not_followed_up() -> None:
    rt, _ = await make_runtime(SUPPORT)
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    r = await say(rt, s.id, "my blender arrived broken")  # asked for the order number; the problem comes first
    assert "probes" not in r.state.data and r.state.open_question and r.state.open_question.field == "order_number", r.outbound.text
    assert "replace" in r.outbound.text.lower(), r.outbound.text

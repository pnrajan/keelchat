"""Replies planned before they are written (`think`), answers from documents only when the documents answer, one
follow-up on a thin form answer (`curious`), and small talk answered in kind (`small_talk`): the model judges, code
decides and counts."""

from __future__ import annotations

from keelchat.evals.generate import targets
from keelchat.policy.compiler import build
from keelchat.testing.fakes import FakeTools
from tests.conftest import ROOT
from tests.helpers import make_runtime, mutate, say

SUPPORT = (ROOT / "examples" / "support_assistant.yaml").read_text()


def _plain(policy: str) -> str:
    """The support example with none of the features, to compare against."""

    def off(d: dict) -> None:  # type: ignore[type-arg]
        step = d["flow"][1]["form_fill"]
        step.pop("think"), step.pop("curious"), d.pop("small_talk")

    return mutate(policy, off)


def test_the_options_compile_and_the_old_hash_stays() -> None:
    on, off = build(SUPPORT), build(_plain(SUPPORT))
    assert on.steps[1].think and on.steps[1].settings["curious"] is True
    assert not off.steps[1].think and off.steps[1].settings["curious"] is False
    # a policy that does not use `think` keeps the hash it had before the option existed
    explicit = mutate(_plain(SUPPORT), lambda d: d["flow"][1]["form_fill"].update(think=False))
    assert off.hash == build(explicit).hash
    assert "probe:issue" in [t.id for t in targets(on)] and not [t for t in targets(off) if t.id.startswith("probe:")]
    assert on.small_talk == "brief" and off.small_talk == "off" and "small_talk" in [t.id for t in targets(on)]


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


async def test_small_talk_is_answered_in_kind_and_counted_after_two_in_a_row() -> None:
    rt, _ = await make_runtime(SUPPORT)
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    for n in (1, 2, 3):
        r = await say(rt, s.id, "haha sorry, how's your day going?")
        assert r.state.data["small_talk"] == n and "order_number" not in r.state.fields and not r.state.reasks, r.outbound.text
        assert r.state.digressions == (0 if n <= 2 else 1)  # the third in a row counts as wandering off
        assert r.state.open_question and r.state.open_question.field == "order_number" and "?" in r.outbound.text  # back to it
    r = await say(rt, s.id, "ORD-12345")
    assert "small_talk" not in r.state.data and r.state.fields["order_number"] == "ORD-12345"


async def test_without_small_talk_a_chat_counts_as_wandering_off() -> None:
    rt, _ = await make_runtime(_plain(SUPPORT))
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    r = await say(rt, s.id, "haha sorry, how's your day going?")
    assert r.state.digressions == 1 and "small_talk" not in r.state.data


def test_only_the_change_lines_an_edit_adds_are_its_changes() -> None:
    from keelchat.policy.drafter import new_changes

    before = "# change: reworded the refund rule (for: 2 failed replies)\nagent: a\n"
    after = "# change: reworded the refund rule (for: 2 failed replies)\n# change: added Spanish\nagent: a\nlanguages: [en, es]\n"
    yaml, changes = new_changes(before, after)
    assert changes == ["added Spanish"]
    assert yaml == "# change: reworded the refund rule (for: 2 failed replies)\nagent: a\nlanguages: [en, es]\n"


async def test_small_talk_leaves_the_off_topic_count_as_it_was() -> None:
    rt, _ = await make_runtime(SUPPORT)
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    r = await say(rt, s.id, "what's the capital of France?")
    assert r.state.digressions == 1, r.outbound.text
    r = await say(rt, s.id, "haha sorry, how's your day going?")
    assert r.state.data.get("small_talk") == 1 and r.state.digressions == 1  # not reset by chit-chat
    r = await say(rt, s.id, "ORD-12345")
    assert "small_talk" not in r.state.data  # any other turn ends the run


async def test_a_follow_up_that_starts_with_no_is_still_kept() -> None:
    rt, _ = await make_runtime(SUPPORT)
    s = await rt.start_session("support-assistant", "u1")
    for m in ("hi", "yes", "ORD-12345"):
        await say(rt, s.id, m)
    r = await say(rt, s.id, "it's broken")
    assert r.state.open_question and r.state.open_question.field == "issue__more", r.outbound.text
    r = await say(rt, s.id, "No power at all, the screen stays black")
    assert "No power at all" in r.state.fields["issue"], r.state.fields


async def test_the_form_ends_with_its_closing() -> None:
    rt, _ = await make_runtime(SUPPORT)
    s = await rt.start_session("support-assistant", "u1")
    for m in ("hi", "yes", "ORD-12345"):
        await say(rt, s.id, m)
    r = await say(rt, s.id, "the glass jug arrived cracked down the side")
    assert r.state.completed and "email" in r.outbound.text.lower() and "working day" in r.outbound.text.lower(), r.outbound.text


async def test_no_answer_says_how_to_reach_a_person_only_when_the_policy_allows_it() -> None:
    benefits = [{"title": "Benefits", "text": "Health, dental and a 401k match from day one.", "source": "hr.md"}]

    async def ask(policy: str) -> str:
        rt, _ = await make_runtime(policy, tools=FakeTools(passages=benefits))
        s = await rt.start_session("support-assistant", "u1")
        for m in ("hi", "yes"):
            await say(rt, s.id, m)
        return (await say(rt, s.id, "do you ship to Canada?")).outbound.text.lower()

    with_handoff = await ask(SUPPORT)
    assert "person" in with_handoff or "colleague" in with_handoff or "human" in with_handoff, with_handoff
    without = await ask(mutate(SUPPORT, lambda d: d.pop("handoff")))
    assert "person" not in without and "human" not in without, without

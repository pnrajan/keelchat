"""Checks every conversation must pass, and generating an eval set from a policy."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from tests.conftest import ROOT
from yachat.evals.checks import code_checks
from yachat.evals.generate import covered, generate, shrink, targets, write
from yachat.evals.metrics import Thresholds
from yachat.evals.replay import Expect, Outcome, ReplayCase, ReplayResult, load_agent_set
from yachat.policy.compiler import build
from yachat.sim.harness import TurnLog

SUPPORT = ROOT / "examples" / "support_assistant.yaml"
#: read at import: conftest clears it for every test
JUDGE = os.getenv("EVALS_JUDGE_MODEL")


def _result(turns: list[TurnLog], expect: Expect | None = None, **state: object) -> ReplayResult:
    base: dict[str, object] = dict(exit_reason=None, completed=False, steps={}, fields={}, digressions=0, nudges=0, reasks={}, language="en")
    outcome = Outcome.model_validate(base | state)
    return ReplayResult(ReplayCase(name="c", messages=[t.user for t in turns], expect=expect or Expect()), turns, outcome)


def test_code_checks_catch_errors_empty_replies_loops_limits_and_language() -> None:
    policy = build(SUPPORT.read_text())
    same = [TurnLog("a", "What is your order number?"), TurnLog("b", "What is your order number?"), TurnLog("c", "What is your order number?")]
    r = _result([TurnLog("hi", "", errors=["intent_detector: LLMError"]), *same], reasks={"order_number": 9}, digressions=9, language="fr")
    assert sorted(v.check for v in code_checks(policy, r)) == ["language_allowed", "no_empty_reply", "no_errors", "no_loop", "within_limits", "within_limits"]
    assert code_checks(policy, _result([TurnLog("hi", "Hello!"), TurnLog("ORD-1", "Thanks.")], reasks={"order_number": 1})) == []


def test_rules_fired_and_not_fired_are_checked_on_any_turn() -> None:
    turns = [TurnLog("hi", "hello"), TurnLog("it broke", "sorry", rules=["damaged_item"])]
    assert _result(turns, Expect(rules_fired=["damaged_item"])).expectation_failures == []
    assert _result(turns, Expect(rules_not_fired=["damaged_item"], rules_fired=["late"])).expectation_failures == [
        "rules never matched: ['late']",
        "rules matched but should not have: ['damaged_item']",
    ]


def test_targets_come_from_the_policy() -> None:
    ids = [t.id for t in targets(build(SUPPORT.read_text()))]
    assert ids[:2] == ["completed", "disengaged"] and "reask:order_number" in ids and "handed_off" in ids and "language:es" in ids
    assert ids.index("rule:damaged_item") + 1 == ids.index("rule_miss:damaged_item")
    concierge = [t.id for t in targets(build((ROOT / "examples" / "concierge.yaml").read_text()))]
    assert "uses_tools" in concierge and "completed" not in concierge  # no flow: nothing to complete


def test_coverage_is_read_from_the_session_and_turns() -> None:
    policy = build(SUPPORT.read_text())
    r = _result(
        [TurnLog("q", "a", tools=["knowledge_search"], tool_outputs=["[]"], rules=["damaged_item"], reasked=["order_number"])],
        steps={"consent": "complete", "issue": "in_progress"},
        exit_reason="handed_off",
        language="es",
    )
    assert covered(policy, r) == {"step:consent", "reask:order_number", "rule:damaged_item", "handed_off", "language:es", "uses_tools"}


async def test_shrink_keeps_the_shortest_script_that_still_fails() -> None:
    calls: list[list[str]] = []

    async def fails(m: list[str]) -> bool:
        calls.append(m)
        return "broken" in m

    short, reproduced = await shrink(["hi", "yes", "broken", "ORD-1", "thanks"], fails)
    assert short == ["broken"] and reproduced and len(calls) <= 6
    assert await shrink(["hi"], fails) == (["hi"], False)


def test_written_set_loads_with_the_hand_written_one(tmp_path: Path) -> None:
    from yachat.evals.generate import GenerateReport

    hand = {"fixtures": {"passages": [{"title": "T", "text": "x", "source": "s"}]}, "cases": [{"name": "mine", "messages": ["hi"]}]}
    (tmp_path / "replay.yaml").write_text(yaml.safe_dump(hand))
    case = ReplayCase(name="gen_handed_off", messages=["hi", "a person please"], expect=Expect(exit_reason="handed_off"), note="covers handed_off")
    report = GenerateReport("support-assistant", ["handed_off"], cases=[case])
    out = write(tmp_path, SUPPORT, report)
    assert out.read_text().startswith("# yachat evals generate: support-assistant: 1 cases")
    merged = load_agent_set(tmp_path)
    assert [c.name for c in merged.cases] == ["mine", "gen_handed_off"] and merged.fixtures.passages
    assert merged.cases[1].expect.exit_reason == "handed_off"
    th = Thresholds.load(tmp_path / "thresholds.yaml")  # written because the folder had none
    assert (tmp_path / th.policy).resolve() == SUPPORT.resolve() and th.minimums == {}
    # a later run for other targets (`--only`) keeps this case; a rerun of the same target replaces it
    write(tmp_path, SUPPORT, GenerateReport("support-assistant", ["completed"], cases=[ReplayCase(name="gen_completed", messages=["hi"])]))
    assert [c.name for c in load_agent_set(tmp_path).cases] == ["mine", "gen_handed_off", "gen_completed"]
    write(tmp_path, SUPPORT, GenerateReport("support-assistant", ["handed_off"]))
    assert [c.name for c in load_agent_set(tmp_path).cases] == ["mine", "gen_completed"]
    (tmp_path / "generated.yaml").write_text(yaml.safe_dump({"cases": [{"name": "mine", "messages": ["x"]}]}))
    with pytest.raises(ValueError, match="used twice"):
        load_agent_set(tmp_path)


def test_a_quote_counts_despite_case_spacing_and_curly_punctuation() -> None:
    from yachat.evals.checks import _quoted

    reply = "We’ll send a free replacement — what’s your   order number?"
    assert _quoted("we'll send a free replacement - what's your order number?", reply)
    assert not _quoted("we'll send a refund", reply) and not _quoted("  ", reply)


async def test_generate_plays_a_target_and_keeps_or_reports_it() -> None:
    """Real model: one target. It is kept as a script, kept as a shortened failure, or reported with a reason."""
    report = await generate(build(SUPPORT.read_text()), only=["handed_off"], rounds=1)
    assert report.targets == ["handed_off"]
    if report.cases:
        (case,) = report.cases
        assert case.name == "gen_handed_off" and case.messages and not case.persona
        if case.note.startswith("covers"):  # stable: replayed as a script it still reaches the target
            assert case.expect.exit_reason == "handed_off"
        else:  # failing, or reached only as played: nothing is expected of it
            assert case.note.startswith(("fails", "covered")) and case.expect == Expect()
    else:
        assert report.not_reached["handed_off"]


@pytest.mark.skipif(not JUDGE, reason="EVALS_JUDGE_MODEL not set")
async def test_in_scope_passes_a_consent_question_and_fails_out_of_scope_work() -> None:
    """The judge grades scope only: asking for consent first is in scope; writing legal advice is not."""
    from yachat.evals.checks import judged_checks

    policy = build(SUPPORT.read_text())
    ok = _result([TurnLog("my order is late", "Sorry about that! Before we start: may I use your answers to help with this?")])
    bad = _result([TurnLog("can you draft a lawsuit against the courier?", "Sure. Here is a complaint you can file in small claims court: ...")])
    assert await judged_checks(policy, ok, str(JUDGE), only={"in_scope"}) == []
    assert [v.check for v in await judged_checks(policy, bad, str(JUDGE), only={"in_scope"})] == ["in_scope"]


@pytest.mark.skipif(not JUDGE, reason="EVALS_JUDGE_MODEL not set")
async def test_rule_followed_counts_what_the_user_already_gave() -> None:
    """`damaged_item` asks for the order number only when it is not known: an earlier turn that gave it counts."""
    from yachat.evals.checks import judged_checks

    policy = build(SUPPORT.read_text())
    turns = [
        TurnLog("my order number is ORD-12345", "Thanks! What went wrong?"),
        TurnLog("the vase arrived shattered", "I'm sorry! We can send you a free replacement for order ORD-12345.", rules=["damaged_item"]),
    ]
    assert await judged_checks(policy, _result(turns), str(JUDGE), only={"rule_followed:damaged_item"}) == []


@pytest.mark.skipif(not JUDGE, reason="EVALS_JUDGE_MODEL not set")
async def test_rule_followed_ignores_what_the_rule_does_not_ask() -> None:
    """Offering the replacement and asking for the order number follows `damaged_item`; never offering it does not."""
    from yachat.evals.checks import judged_checks

    policy = build(SUPPORT.read_text())
    msg = "my blender arrived broken, the jug is cracked"
    ok = _result([TurnLog(msg, "I'm sorry! We can send a free replacement. What is your order number?", rules=["damaged_item"])])
    bad = _result([TurnLog(msg, "That's unfortunate. Please check our returns page.", rules=["damaged_item"])])
    assert await judged_checks(policy, ok, str(JUDGE), only={"rule_followed:damaged_item"}) == []
    assert len(await judged_checks(policy, bad, str(JUDGE), only={"rule_followed:damaged_item"})) == 1

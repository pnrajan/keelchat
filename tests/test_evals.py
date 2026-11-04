"""Eval harness: replay sets on the simulator's engine, expectations, metric names and the gate, judged scoring."""

from __future__ import annotations

import json
import os

import pytest
import yaml
from pydantic import ValidationError

from tests.conftest import ROOT
from yachat.evals.metrics import CATALOGUE, MetricScore, Thresholds, gate, online_metrics
from yachat.evals.online import OutcomeSplit, export_replay, redact, redact_session
from yachat.evals.replay import Expect, Outcome, ReplayCase, ReplayResult, load_cases, load_set, run_set
from yachat.evals.run import evaluate, score
from yachat.policy.compiler import build
from yachat.sim.harness import TurnLog
from yachat.state.models import SessionState, Turn

EVALS = ROOT / "evals"
#: read at import: conftest clears it for every test so only this file's judged test calls a judge
JUDGE = os.getenv("EVALS_JUDGE_MODEL")


async def _set(name: str) -> list[ReplayResult]:
    """The set's scripted cases. Persona cases vary run to run: `yachat evals run` covers them, not the unit suite."""
    th = Thresholds.load(EVALS / name / "thresholds.yaml")
    replay = load_set(EVALS / name / "replay.yaml")
    replay.cases = [c for c in replay.cases if not c.persona]
    return await run_set(build((EVALS / name / th.policy).read_text()), replay)


async def test_hiring_replay_set_expectations_hold() -> None:
    results = await _set("hiring_agent")
    assert {r.case.name: r.expectation_failures for r in results if r.expectation_failures} == {}
    happy = next(r for r in results if r.case.name == "happy_path")
    assert happy.turns[2].tools == ["job_search"] and happy.turns[2].tool_outputs


async def test_career_coach_replay_set() -> None:
    assert all(not r.expectation_failures for r in await _set("career_coach"))


async def test_support_set_checks_rules_and_tools() -> None:
    results = {r.case.name: r for r in await _set("support_assistant")}
    assert {n: r.expectation_failures for n, r in results.items() if r.expectation_failures} == {}
    rule = results["damaged_item_rule"]
    assert rule.turns[2].rules == ["damaged_item"] and rule.turns[3].rules == []
    returns = results["returns_question"].turns[-1]
    assert "30 days" in returns.tool_outputs[0]  # the eval set's own fixture passages, as JSON
    assert json.loads(returns.tool_outputs[0])[0]["source"] == "help/returns"


async def test_concierge_set_retrieves_from_the_sets_documents() -> None:
    results = await _set("concierge")
    assert {r.case.name: r.expectation_failures for r in results if r.expectation_failures} == {}
    pool = next(r for r in results if r.case.name == "pool_hours")
    assert any("rooftop pool" in o for o in pool.turns[0].tool_outputs)  # what the reply says is scored, not asserted


def _result(turns: list[TurnLog], expect: Expect, exit_reason: str | None = None) -> ReplayResult:
    outcome = Outcome(exit_reason=exit_reason, completed=False, steps={}, fields={}, digressions=0, nudges=0, reasks={}, language="en")
    return ReplayResult(ReplayCase(name="c", messages=[t.user for t in turns], expect=expect), turns, outcome)


def test_expectations_check_rules_per_turn_and_state() -> None:
    turns = [TurnLog("hi", "hello"), TurnLog("it arrived broken", "sorry", rules=["damaged_item"])]
    assert _result(turns, Expect(rules={2: ["damaged_item"], 1: []})).expectation_failures == []
    bad = _result(turns, Expect(rules={1: ["damaged_item"], 3: []}, exit_reason="completed"), "max_reasks")
    assert sorted(bad.expectation_failures) == [
        "exit_reason: expected 'completed', got 'max_reasks'",
        "rules on turn 1: expected ['damaged_item'], got []",
        "rules on turn 3: expected [], got None",
    ]


def test_deepeval_test_case_conversion() -> None:
    pytest.importorskip("deepeval")
    r = _result([TurnLog("hi", "hello"), TurnLog("benefits?", "Health and dental.", ["knowledge_search"], ["We offer health, dental"])], Expect())
    tc = r.to_conversational_test_case()
    assert len(tc.turns) == 4 and tc.turns[0].role == "user"
    assert r.to_llm_test_cases()[1].retrieval_context == ["We offer health, dental"]


def test_every_catalogue_metric_exists_in_deepeval() -> None:
    dm = pytest.importorskip("deepeval.metrics")
    assert [cls for _, cls, _ in CATALOGUE.values() if not hasattr(dm, cls)] == []


def test_cases_and_thresholds_are_validated() -> None:
    with pytest.raises(ValidationError):
        ReplayCase(name="x", messages=["hi"], persona="someone")
    with pytest.raises(ValidationError):
        ReplayCase(name="x", messages=["hi"], expect={"exit": "completed"})  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        Thresholds.model_validate({"minimums": {}})  # the policy under test is required


def test_gate_knows_catalogue_rubrics_rules_and_ragas() -> None:
    th = Thresholds.load(EVALS / "hiring_agent" / "thresholds.yaml")
    assert "natural_tone" in th.rubrics and "natural_tone" not in CATALOGUE  # rubrics live in the eval set
    scores = [MetricScore(n, 0.95, True) for n in th.minimums]
    assert gate(scores, th) == []
    scores[0] = MetricScore(scores[0].metric, 0.1, False, "bad")
    assert len(gate(scores, th)) == 1
    other = Thresholds(policy="p.yaml", minimums={"nope": 0.5, "rule_followed": 0.8, "ragas.Faithfulness": 0.8})
    assert gate([], other) == ["nope: not a known metric", "rule_followed: no score produced", "ragas.Faithfulness: no score produced"]
    assert "tool_correctness" not in online_metrics(list(CATALOGUE)) and "faithfulness" in online_metrics(list(CATALOGUE))


async def test_evaluate_without_judge_checks_expectations_only() -> None:
    report = await evaluate(EVALS / "career_coach", judge=None)
    assert report.ok and not report.judged and report.agent == "career-coach" and report.cases == 2


def test_ragas_context_is_the_listed_tools_output() -> None:
    from yachat.evals.run import _context

    t = TurnLog("q", "a", ["job_search", "knowledge_search"], ["[jobs]", "[passages]"])
    assert _context(t, ["knowledge_search"]) == ["[passages]"] and _context(t, None) == ["[jobs]", "[passages]"]


@pytest.mark.skipif(not JUDGE, reason="EVALS_JUDGE_MODEL not set")
async def test_judged_ragas_and_rule_scores() -> None:
    """The judge scores a faithful answer and a reply that follows its rule high, and an invented one low."""
    policy = build((ROOT / "examples" / "support_assistant.yaml").read_text())
    passage = "Items can be returned within 30 days of delivery for a full refund."

    def one(agent: str, rules: list[str], outputs: list[str]) -> ReplayResult:
        return _result([TurnLog("my blender arrived broken; how long do I have to return it?", agent, ["knowledge_search"], outputs, rules)], Expect())

    th = Thresholds(policy="", minimums={"ragas.Faithfulness": 0.5, "rule_followed": 0.5})
    model = str(JUDGE)
    good = {
        s.metric: s
        for s in await score(
            [one("Sorry about that! We can send a free replacement, and you have 30 days to return it.", ["damaged_item"], [passage])], th, policy, model
        )
    }
    bad = {s.metric: s for s in await score([one("You have 90 days and we will pay for a new kitchen.", ["damaged_item"], [passage])], th, policy, model)}
    assert good["ragas.Faithfulness"].score > bad["ragas.Faithfulness"].score, (good, bad)
    assert good["rule_followed"].score > bad["rule_followed"].score, (good, bad)
    assert good["rule_followed"].passed and not bad["rule_followed"].passed


async def test_a_metric_the_harness_cannot_build_fails_alone() -> None:
    pytest.importorskip("ragas")
    th = Thresholds(policy="", minimums={"ragas.AnswerRelevancy": 0.5, "ragas.NoSuchMetric": 0.5, "non_advice": 0.5})
    out = {s.metric: s for s in await score([], th, build((ROOT / "examples" / "support_assistant.yaml").read_text()), "unused/model")}
    assert not out["ragas.AnswerRelevancy"].passed and "needs more than a judge" in out["ragas.AnswerRelevancy"].reason
    assert "no such Ragas metric" in out["ragas.NoSuchMetric"].reason
    assert "needs options" in out["non_advice"].reason  # advice_types comes from thresholds.yaml `options:`


def test_redaction_outcome_split_and_export(tmp_path) -> None:  # type: ignore[no-untyped-def]
    st = SessionState(user_id="u", agent="hiring-agent", policy_hash="h", channel="web")
    st.turns = [Turn(role="user", text="I am Jane, jane@x.com, 12 Elm Street Austin"), Turn(role="agent", text="ok")]
    st.digressions, st.reasks, st.exit_reason = 2, {"address": 1}, "disengaged"
    row = redact_session(st)
    assert "jane@x.com" not in row["turns"][0]["text"] and "[email]" in row["turns"][0]["text"] and "fields" not in row
    assert redact("call 512-555-1234") == "call [phone]"
    rows = [dict(row, submitted=True, digressions=0), dict(row, submitted=False, digressions=3), dict(row, submitted=True, digressions=1)]
    summary = OutcomeSplit(rows).compute()
    assert summary["digression_lift"] is not None and "address" in summary["dropoff_after_failed_answer"]
    case = export_replay(st, "live-1")
    assert case["messages"] == ["I am Jane, [email], [address] Austin"] and case["expect"] == {"exit_reason": "disengaged"}
    out = tmp_path / "replay.yaml"
    out.write_text(yaml.safe_dump({"cases": [case]}))
    assert load_cases(out)[0].name == "live-1"

from __future__ import annotations

import asyncio

import pytest
import yaml

from evals.metrics import CATALOGUE, MetricScore, Thresholds, gate, online_metrics
from evals.online import OutcomeSplit, export_replay, redact, redact_session
from evals.personas import personas
from evals.replay import load_cases, run_case, run_replay_set
from evals.run import evaluate
from tests.conftest import ROOT
from yachat.state.models import SessionState, Turn


async def test_replay_set_expectations_hold():
    results = await run_replay_set(ROOT / "examples" / "hiring_agent.yaml", ROOT / "evals" / "hiring_agent" / "replay.yaml")
    failures = {r.case.name: r.expectation_failures for r in results if r.expectation_failures}
    assert failures == {}
    happy = next(r for r in results if r.case.name == "happy_path")
    assert happy.turns[2].tools == ["job_search"] and happy.turns[2].tool_outputs


async def test_career_coach_replay():
    results = await run_replay_set(ROOT / "examples" / "career_coach.yaml", ROOT / "evals" / "career_coach" / "replay.yaml")
    assert all(not r.expectation_failures for r in results)


async def test_personas_run_offline():
    policy = (ROOT / "examples" / "hiring_agent.yaml").read_text()
    reports = {p.name: await run_case(policy, p.as_case()) for p in personas()}
    assert reports["chatty_non_serious"].state["exit_reason"] == "disengaged"
    assert reports["spanish_typos"].state["language"] == "es"
    assert reports["sms_shorthand"].turns[2].tools == ["job_search"]


def test_deepeval_test_case_conversion():
    pytest.importorskip("deepeval")
    policy = (ROOT / "examples" / "hiring_agent.yaml").read_text()
    case = load_cases(ROOT / "evals" / "hiring_agent" / "replay.yaml")[0]
    r = asyncio.run(run_case(policy, case))
    tc = r.to_conversational_test_case()
    assert len(tc.turns) == 2 * len(r.turns) and tc.turns[0].role == "user"
    single = r.to_llm_test_cases()
    assert single[2].retrieval_context and "Backend Engineer" in single[2].retrieval_context[0]


def test_gate_and_online_subset(tmp_path):
    th = Thresholds.load(ROOT / "evals" / "hiring_agent" / "thresholds.yaml")
    scores = [MetricScore(n, 0.95, True) for n in th.minimums]
    assert gate(scores, th) == []
    scores[0] = MetricScore(scores[0].metric, 0.1, False, "bad")
    assert len(gate(scores, th)) == 1
    assert gate([], Thresholds("x", {"nope": 0.5})) == ["nope: not a known metric"]
    assert "tool_correctness" not in online_metrics(list(CATALOGUE)) and "faithfulness" in online_metrics(list(CATALOGUE))


async def test_evaluate_without_judge_checks_expectations_only():
    report = await evaluate(ROOT / "evals" / "hiring_agent", judge=None, with_personas=False)
    assert report.ok and not report.judged and report.cases >= 10


def test_redaction_outcome_split_and_export(tmp_path):
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

"""Repeated runs (a case passes only when every run does) and the judge's agreement with labels a person wrote."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from tests.conftest import ROOT
from yachat.evals.calibrate import LABELS, Label, LabelSet, Turn, calibrate, group
from yachat.evals.calibrate import _validate as validate_labels
from yachat.evals.run import EvalReport, evaluate
from yachat.policy.compiler import build

SUPPORT = ROOT / "examples" / "support_assistant.yaml"
#: read at import: conftest clears it for every test
JUDGE = os.getenv("EVALS_JUDGE_MODEL")


def test_a_case_passes_only_when_every_trial_does() -> None:
    report = EvalReport("a", cases=3, trials=3)
    for _ in range(3):
        report.add("steady", [], [])
    report.add("flaky", [], [])
    report.add("flaky", ["completed: expected True, got False"], [])
    report.add("flaky", [], ["no_loop (turn 3): the same reply 3 turns in a row"])
    for _ in range(3):
        report.add("broken", [], ["no_empty_reply (turn 1): the agent sent nothing"])
    assert report.passes == {"steady": 3, "flaky": 1, "broken": 0}
    assert report.flaky == {"flaky": "1/3"} and not report.ok
    assert report.expectation_failures == {"flaky": ["trial 2: completed: expected True, got False"]}
    assert report.check_failures["flaky"] == ["trial 3: no_loop (turn 3): the same reply 3 turns in a row"]
    assert report.to_dict()["passed_every_trial"] == "1/3"
    one = EvalReport("a", cases=1)
    one.add("c", ["x"], [])
    assert one.expectation_failures == {"c": ["x"]}  # one trial: no tag


def test_labels_load_and_name_a_check_the_policy_has() -> None:
    policy = build(SUPPORT.read_text())
    labels = LabelSet.load(ROOT / "evals" / "support_assistant" / LABELS)
    validate_labels(policy, labels)
    by = {g: [lb.passes for lb in labels.labels if group(lb.check) == g] for g in ("in_scope", "rule_followed")}
    assert all(len(v) >= 20 and 0 < sum(v) < len(v) for v in by.values())  # enough of each, passes and fails
    bad = LabelSet(labels=[Label.model_validate({"check": "rule_followed:nope", "turns": [Turn(user="a", agent="b")], "pass": True})])
    with pytest.raises(ValueError, match=r"labels\[0\]\.check"):
        validate_labels(policy, bad)


async def test_trials_run_each_case_that_many_times(tmp_path: Path) -> None:
    """Real model, no judge: two runs of one short script."""
    (tmp_path / "thresholds.yaml").write_text(yaml.safe_dump({"policy": os.path.relpath(SUPPORT, tmp_path), "minimums": {}, "trials": 2}))
    (tmp_path / "replay.yaml").write_text(yaml.safe_dump({"cases": [{"name": "hello", "messages": ["hi"]}]}))
    report = await evaluate(tmp_path, judge=None)
    assert report.trials == 2 and report.cases == 1 and report._runs == {"hello": 2}
    assert report.to_dict()["passed_every_trial"] in ("0/1", "1/1")


@pytest.mark.skipif(not JUDGE, reason="EVALS_JUDGE_MODEL not set")
async def test_calibrate_counts_agreement_per_check() -> None:
    """Clear-cut labels: the judge should agree with all of them."""
    policy = build(SUPPORT.read_text())
    labels = LabelSet.load(ROOT / "evals" / "support_assistant" / LABELS)
    picked = [lb for lb in labels.labels if lb.note in ("legal advice", "declines the legal part and redirects", "the reply the generated set caught")]
    picked.append(labels.labels[next(i for i, lb in enumerate(labels.labels) if lb.check.startswith("rule_followed"))])
    report = await calibrate(policy, LabelSet(labels=picked), str(JUDGE))
    assert set(report.checks) == {"in_scope", "rule_followed"} and not report.errors
    assert sum(a.labels for a in report.checks.values()) == 4
    assert report.disagreements == [] and report.ok, report.summary()

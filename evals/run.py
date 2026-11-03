"""Offline evaluation (CI gate): replay set + personas → expectations, and DeepEval metrics when a judge is configured."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from evals.judge import judge_model_from_env
from evals.metrics import MetricScore, Thresholds, build_metric, gate
from evals.personas import personas
from evals.replay import ReplayResult, load_cases, run_case

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class EvalReport:
    agent: str
    cases: int
    expectation_failures: dict[str, list[str]] = dc_field(default_factory=dict)
    metric_scores: list[MetricScore] = dc_field(default_factory=list)
    gate_failures: list[str] = dc_field(default_factory=list)
    judged: bool = False

    @property
    def ok(self) -> bool:
        return not self.expectation_failures and not self.gate_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "cases": self.cases,
            "ok": self.ok,
            "judged": self.judged,
            "expectation_failures": self.expectation_failures,
            "metric_scores": [asdict(s) for s in self.metric_scores],
            "gate_failures": self.gate_failures,
        }


async def score_with_deepeval(results: list[ReplayResult], thresholds: Thresholds, judge: Any) -> list[MetricScore]:
    """Average each metric over the replay set. Conversational metrics on ConversationalTestCase; single-turn ones per agent turn."""
    from evals.metrics import CATALOGUE

    scores: list[MetricScore] = []
    for name, minimum in thresholds.minimums.items():
        if name not in CATALOGUE:
            continue
        kind = CATALOGUE[name][0]
        metric = build_metric(name, minimum, judge)
        values: list[float] = []
        reasons: list[str] = []
        for r in results:
            cases = r.to_llm_test_cases() if kind == "single" else [r.to_conversational_test_case()]
            for tc in cases:
                try:
                    await metric.a_measure(tc)
                    values.append(float(metric.score or 0.0))
                    if metric.reason:
                        reasons.append(str(metric.reason)[:120])
                except Exception as e:  # a judge failure is a failed metric, not a crashed run
                    values.append(0.0)
                    reasons.append(f"{e.__class__.__name__}")
        avg = sum(values) / len(values) if values else 0.0
        scores.append(MetricScore(name, avg, avg >= minimum, reasons[0] if reasons else ""))
    return scores


FROM_ENV = "env"


async def evaluate(agent_dir: str | Path, judge: Any | None = FROM_ENV, with_personas: bool = True) -> EvalReport:
    """`judge=None` runs structural expectations only; the default resolves a judge from EVALS_JUDGE_MODEL when set."""
    agent_dir = Path(agent_dir)
    thresholds = Thresholds.load(agent_dir / "thresholds.yaml")
    policy_path = ROOT / "examples" / f"{thresholds.agent.replace('-', '_')}.yaml"
    policy_text = policy_path.read_text()
    cases = load_cases(agent_dir / "replay.yaml")
    if with_personas and (agent_dir / "personas.yaml").exists():
        import yaml

        names = yaml.safe_load((agent_dir / "personas.yaml").read_text()).get("personas", [])
        cases += [p.as_case() for p in personas(names)]
    results = [await run_case(policy_text, c) for c in cases]
    report = EvalReport(agent=thresholds.agent, cases=len(results))
    for r in results:
        if r.expectation_failures:
            report.expectation_failures[r.case.name] = r.expectation_failures
    if judge == FROM_ENV:
        judge = judge_model_from_env()
    if judge is not None:
        report.judged = True
        report.metric_scores = await score_with_deepeval(results, thresholds, judge)
        report.gate_failures = gate(report.metric_scores, thresholds)
    return report


def main(argv: list[str]) -> int:
    agent_dirs = [Path(a) for a in argv] or sorted(p for p in (ROOT / "evals").iterdir() if (p / "thresholds.yaml").exists())
    ok = True
    for d in agent_dirs:
        report = asyncio.run(evaluate(d))
        print(json.dumps(report.to_dict(), indent=2))
        ok = ok and report.ok
    return 0 if ok else 1

"""Offline evaluation (CI gate): an eval set's replay cases → expectations checked in code, and DeepEval, Ragas and
rule metrics when a judge model is configured."""

from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

from yachat.evals.checks import check, code_checks, rules_by_name
from yachat.evals.judge import deepeval_judge, judge_model_from_env, ragas_judge
from yachat.evals.metrics import CATALOGUE, RAGAS, RULE_FOLLOWED, MetricScore, Thresholds, build_metric, earlier, gate, ragas_metric, rule_metric
from yachat.evals.replay import ReplayResult, load_agent_set, run_set
from yachat.policy.compiler import CompiledPolicy, build
from yachat.sim.harness import TurnLog

#: inputs a turn gives a Ragas metric
RAGAS_INPUTS = {"user_input", "response", "retrieved_contexts"}
_NAMED = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
#: characters of a judge's reason kept in the report
REASON_CHARS = 120


@dataclass
class EvalReport:
    agent: str
    cases: int
    expectation_failures: dict[str, list[str]] = dc_field(default_factory=dict)
    #: case → checks every conversation must pass (yachat.evals.checks)
    check_failures: dict[str, list[str]] = dc_field(default_factory=dict)
    metric_scores: list[MetricScore] = dc_field(default_factory=list)
    gate_failures: list[str] = dc_field(default_factory=list)
    judged: bool = False
    #: gated metrics with no recorded agreement with human labels: treat their scores as advisory (docs/EVALS.md)
    uncalibrated: list[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.expectation_failures and not self.check_failures and not self.gate_failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "cases": self.cases,
            "ok": self.ok,
            "judged": self.judged,
            "expectation_failures": self.expectation_failures,
            "check_failures": self.check_failures,
            "metric_scores": [asdict(s) for s in self.metric_scores],
            "gate_failures": self.gate_failures,
            "uncalibrated": self.uncalibrated,
        }


class _Average:
    def __init__(self, name: str, minimum: float) -> None:
        self.name, self.minimum = name, minimum
        self.values: list[float] = []
        self.reasons: list[str] = []

    async def add(self, measure: Any) -> None:
        """Await one measurement → (score, reason). A judge failure is a failed measurement, not a crashed run."""
        try:
            score, reason = await measure
            self.values.append(float(score or 0.0))
            if reason:
                self.reasons.append(str(reason)[:REASON_CHARS])
        except Exception as e:  # any judge or library error scores 0, with its class as the reason
            self.values.append(0.0)
            self.reasons.append(e.__class__.__name__)

    def score(self) -> MetricScore | None:
        if not self.values:
            return None
        avg = sum(self.values) / len(self.values)
        return MetricScore(self.name, avg, avg >= self.minimum, self.reasons[0] if self.reasons else "")


async def _deepeval(metric: Any, case: Any) -> tuple[float, str]:
    await metric.a_measure(case)
    return metric.score, metric.reason or ""


async def _ragas(metric: Any, **inputs: Any) -> tuple[float, str]:
    r = await metric.ascore(**inputs)
    return r.value, r.reason or ""


def _context(t: TurnLog, tools: list[str] | None) -> list[str]:
    """The turn's tool output that counts as the source: from `tools`, or from every tool when unset."""
    return [o for n, o in zip(t.tools, t.tool_outputs, strict=True) if tools is None or n in tools]


async def score(results: list[ReplayResult], thresholds: Thresholds, policy: CompiledPolicy, model: str) -> list[MetricScore]:
    """Each metric averaged over what it measures: conversations, turns, rule turns or turns with tool output."""
    deepeval_llm = ragas_llm = None
    out: list[MetricScore] = []
    for name, minimum in thresholds.minimums.items():
        if not thresholds.known(name):
            continue
        avg = _Average(name, minimum)
        if name.startswith(RAGAS):
            ragas_llm = ragas_llm or ragas_judge(model)
            try:
                metric = ragas_metric(name, ragas_llm)
            except ValueError as e:
                out.append(MetricScore(name, 0.0, False, str(e)))
                continue
            needs = {p.name for p in inspect.signature(metric.ascore).parameters.values() if p.default is p.empty and p.kind in _NAMED}
            if missing := needs - RAGAS_INPUTS:
                out.append(MetricScore(name, 0.0, False, f"needs {sorted(missing)}, which replay turns do not have"))
                continue
            for r in results:
                for t in r.turns:
                    if (context := _context(t, thresholds.context_tools)) and t.agent:
                        inputs = {"user_input": t.user, "response": t.agent, "retrieved_contexts": context}
                        await avg.add(_ragas(metric, **{k: v for k, v in inputs.items() if k in needs}))
        elif name == RULE_FOLLOWED:
            from deepeval.test_case import LLMTestCase

            deepeval_llm = deepeval_llm or deepeval_judge(model)
            rules = rules_by_name(policy)
            for r in results:
                for i, t in enumerate(r.turns):
                    case = LLMTestCase(input=t.user, actual_output=t.agent, context=earlier(r.turns, i))
                    for rule in (rules[n] for n in t.rules if n in rules):
                        await avg.add(_deepeval(rule_metric(rule.then, minimum, deepeval_llm), case))
        else:
            deepeval_llm = deepeval_llm or deepeval_judge(model)
            try:
                metric = build_metric(name, minimum, deepeval_llm, thresholds.rubrics, thresholds.options.get(name))
            except ValueError as e:
                out.append(MetricScore(name, 0.0, False, str(e)))
                continue
            conversational = name in thresholds.rubrics or CATALOGUE[name][0] == "conversational"
            for r in results:
                for tc in [r.to_conversational_test_case(policy.charter)] if conversational else r.to_llm_test_cases():
                    await avg.add(_deepeval(metric, tc))
        if (s := avg.score()) is not None:
            out.append(s)
    return out


FROM_ENV = "env"


async def evaluate(agent_dir: str | Path, judge: str | None = FROM_ENV, scores: bool = True) -> EvalReport:
    """`judge` is the judge model id; None checks in code only; the default reads EVALS_JUDGE_MODEL. With `scores`, the
    judge scores the thresholds' metrics; without, it runs the judged checks on every turn instead (`yachat evals check`)."""
    agent_dir = Path(agent_dir)
    thresholds = Thresholds.load(agent_dir / "thresholds.yaml")
    policy = build((agent_dir / thresholds.policy).read_text())
    results = await run_set(policy, load_agent_set(agent_dir))
    report = EvalReport(agent=policy.agent, cases=len(results))
    model = judge_model_from_env() if judge == FROM_ENV else judge
    for r in results:
        if r.expectation_failures:
            report.expectation_failures[r.case.name] = r.expectation_failures
        found = code_checks(policy, r) if scores or not model else [v for v in await check(policy, r, model) if v.check != "expect"]
        if found:
            report.check_failures[r.case.name] = [str(v) for v in found]
    report.judged = bool(model)
    if model and scores:
        report.metric_scores = await score(results, thresholds, policy, model)
        report.gate_failures = gate(report.metric_scores, thresholds)
        report.uncalibrated = [m for m in thresholds.minimums if m not in thresholds.calibration]
    return report


def main(argv: list[str], scores: bool = True) -> int:
    """Each argument is an eval set folder; none: every folder under ./evals with a thresholds.yaml."""
    agent_dirs = [Path(a) for a in argv] or sorted(p.parent for p in Path("evals").glob("*/thresholds.yaml"))
    if not agent_dirs:
        print("no eval sets: pass a folder with replay.yaml and thresholds.yaml, or run from a directory with evals/<agent>/")
        return 2
    ok = True
    for d in agent_dirs:
        report = asyncio.run(evaluate(d, scores=scores))
        print(json.dumps(report.to_dict(), indent=2))
        ok = ok and report.ok
    return 0 if ok else 1

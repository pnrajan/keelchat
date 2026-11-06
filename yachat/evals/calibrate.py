"""How often the judge agrees with a person: replies a person marked pass or fail (`evals/<agent>/labels.yaml`) go
through the same judged checks, and the agreement per check is what `thresholds.yaml: calibration` records.

A judged check whose agreement is unknown or low is advice, not a gate.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from yachat.evals.checks import judged_checks, rules_by_name
from yachat.evals.metrics import Thresholds
from yachat.evals.replay import PARALLEL, Outcome, ReplayCase, ReplayResult
from yachat.policy.compiler import CompiledPolicy, build
from yachat.sim.harness import TurnLog

LABELS = "labels.yaml"
#: agreement a judged check needs before it gates
CALIBRATED = 0.8
#: fewer labels than this per check and the agreement is only a rough guide
MIN_LABELS = 20


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user: str
    agent: str


class Label(BaseModel):
    """One reply a person judged: the last of `turns` (the earlier ones are its context)."""

    model_config = ConfigDict(extra="forbid")

    #: `in_scope` or `rule_followed:<rule>`
    check: str
    turns: list[Turn] = Field(min_length=1)
    #: the person's verdict: does the reply pass this check
    passes: bool = Field(alias="pass")
    note: str = ""


class LabelSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labels: list[Label]

    @classmethod
    def load(cls, path: str | Path) -> LabelSet:
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


def group(check: str) -> str:
    """The name a check's agreement is recorded under: `rule_followed:<rule>` counts toward `rule_followed`."""
    return check.split(":", 1)[0]


@dataclass
class Agreement:
    labels: int = 0
    agree: int = 0
    #: the person failed it, the judge passed it
    too_lenient: int = 0
    #: the person passed it, the judge failed it
    too_strict: int = 0

    @property
    def rate(self) -> float:
        return self.agree / self.labels if self.labels else 0.0


@dataclass
class CalibrationReport:
    agent: str
    judge: str
    checks: dict[str, Agreement] = dc_field(default_factory=dict)
    #: labels where the judge and the person differ, with the judge's reason
    disagreements: list[str] = dc_field(default_factory=list)
    #: labels the judge could not grade (left out of the agreement)
    errors: list[str] = dc_field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(a.rate >= CALIBRATED for a in self.checks.values())

    def summary(self) -> str:
        lines = [f"{self.agent}: judge {self.judge}"]
        for name, a in self.checks.items():
            flag = "" if a.rate >= CALIBRATED else f"  below {CALIBRATED:.0%}: advice only, not a gate"
            few = f"  (only {a.labels} labels; aim for {MIN_LABELS}+)" if a.labels < MIN_LABELS else ""
            lines.append(f"  {name}: {a.agree}/{a.labels} agree ({a.rate:.0%}) · too lenient {a.too_lenient} · too strict {a.too_strict}{flag}{few}")
        lines += [f"  differs {d}" for d in self.disagreements]
        lines += [f"  error {e}" for e in self.errors]
        if self.checks:
            lines.append("record in thresholds.yaml:")
            lines.append(yaml.safe_dump({"calibration": {n: round(a.rate, 2) for n, a in self.checks.items()}}, sort_keys=False).rstrip())
        return "\n".join(lines)


def _validate(policy: CompiledPolicy, labels: LabelSet) -> None:
    rules = rules_by_name(policy)
    for i, lb in enumerate(labels.labels):
        kind, _, rule = lb.check.partition(":")
        if lb.check != "in_scope" and not (kind == "rule_followed" and rule in rules):
            raise ValueError(f"labels[{i}].check: {lb.check!r} is not in_scope or rule_followed:<one of {sorted(rules)}>")


def _result(lb: Label) -> ReplayResult:
    rule = lb.check.partition(":")[2]
    turns = [TurnLog(t.user, t.agent) for t in lb.turns]
    if rule:
        turns[-1].rules = [rule]
    state = Outcome(exit_reason=None, completed=False, steps={}, fields={}, digressions=0, nudges=0, reasks={}, language=None)
    return ReplayResult(ReplayCase(name="label", messages=[t.user for t in turns]), turns, state)


async def calibrate(policy: CompiledPolicy, labels: LabelSet, judge: str, parallel: int = PARALLEL) -> CalibrationReport:
    """Run the judged check of every label on its last turn and count where the judge agrees with the person."""
    _validate(policy, labels)
    report = CalibrationReport(policy.agent, judge)
    gate = asyncio.Semaphore(parallel)

    async def one(lb: Label) -> list[str]:
        async with gate:
            return [v.detail for v in await judged_checks(policy, _result(lb), judge, only={lb.check}, turn=len(lb.turns))]

    found = await asyncio.gather(*(one(lb) for lb in labels.labels))
    for i, (lb, details) in enumerate(zip(labels.labels, found, strict=True)):
        where = f"labels[{i}] {lb.check}{f' ({lb.note})' if lb.note else ''}"
        if any(d.startswith("judge failed") for d in details):
            report.errors.append(f"{where}: {'; '.join(details)}")
            continue
        a = report.checks.setdefault(group(lb.check), Agreement())
        judge_passes = not details
        a.labels += 1
        if judge_passes == lb.passes:
            a.agree += 1
            continue
        if judge_passes:
            a.too_lenient += 1
        else:
            a.too_strict += 1
        why = f": {details[0]}" if details else ""
        report.disagreements.append(f"{where}: person {'pass' if lb.passes else 'fail'}, judge {'pass' if judge_passes else 'fail'}{why}")
    return report


def main(argv: list[str]) -> int:
    """`yachat evals calibrate [dirs]`: each folder needs thresholds.yaml and labels.yaml. 1 when a check agrees less
    than CALIBRATED, 2 when there is nothing to run."""
    from yachat.evals.judge import judge_model_from_env

    judge = judge_model_from_env()
    if not judge:
        print("set EVALS_JUDGE_MODEL: calibration measures that judge", file=sys.stderr)
        return 2
    dirs = [Path(a) for a in argv] or sorted(p.parent for p in Path("evals").glob(f"*/{LABELS}"))
    if not dirs:
        print(f"no {LABELS}: label replies in evals/<agent>/{LABELS} (docs/EVALS.md#calibration)", file=sys.stderr)
        return 2
    ok = True
    for d in dirs:
        thresholds = Thresholds.load(d / "thresholds.yaml")
        policy = build((d / thresholds.policy).read_text())
        report = asyncio.run(calibrate(policy, LabelSet.load(d / LABELS), judge))
        print(report.summary())
        ok = ok and report.ok
    return 0 if ok else 1

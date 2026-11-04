"""Metric names, thresholds and the CI gate.

A metric in `thresholds.yaml` is one of:
- a DeepEval metric from `CATALOGUE` (`role_adherence`, `faithfulness`, ...);
- a rubric the eval set defines under `rubrics:` (scored with DeepEval's ConversationalGEval);
- `rule_followed`: on each turn where a policy rule matched, does the reply do what the rule says;
- `ragas.<Metric>`: any Ragas collections metric whose inputs a turn has (user_input, response, retrieved_contexts),
  scored on the turns where a `context_tools` tool returned something, e.g. `ragas.Faithfulness`, `ragas.ContextPrecisionWithoutReference`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

#: DeepEval metric name → (kind, deepeval class name, online-safe)
CATALOGUE: dict[str, tuple[str, str, bool]] = {
    "conversation_completeness": ("conversational", "ConversationCompletenessMetric", True),
    "knowledge_retention": ("conversational", "KnowledgeRetentionMetric", True),
    "role_adherence": ("conversational", "RoleAdherenceMetric", True),
    "conversation_relevancy": ("conversational", "TurnRelevancyMetric", True),
    "faithfulness": ("single", "FaithfulnessMetric", True),
    "tool_correctness": ("single", "ToolCorrectnessMetric", False),
    "argument_correctness": ("single", "ArgumentCorrectnessMetric", False),
    "task_completion": ("single", "TaskCompletionMetric", True),
    "pii_leakage": ("single", "PIILeakageMetric", True),
    "non_advice": ("single", "NonAdviceMetric", True),
    "role_violation": ("single", "RoleViolationMetric", True),
}

RULE_FOLLOWED = "rule_followed"
RAGAS = "ragas."


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: the policy under test, relative to the eval set's folder
    policy: str
    minimums: dict[str, float]
    #: tools whose output counts as the source for faithfulness-style metrics; unset: every tool
    context_tools: list[str] | None = None
    #: rubric name → what a good conversation does (ConversationalGEval criteria)
    rubrics: dict[str, str] = Field(default_factory=dict)
    #: metric → extra constructor arguments, e.g. `non_advice: {advice_types: [medical]}`
    options: dict[str, dict[str, Any]] = Field(default_factory=dict)
    #: metric → agreement with the human-labeled set (docs/EVALS.md)
    calibration: dict[str, float] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Thresholds:
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

    def known(self, name: str) -> bool:
        return name in CATALOGUE or name in self.rubrics or name == RULE_FOLLOWED or name.startswith(RAGAS)


@dataclass
class MetricScore:
    metric: str
    score: float
    passed: bool
    reason: str = ""


def gate(scores: list[MetricScore], thresholds: Thresholds) -> list[str]:
    """Failures: any metric below its minimum. Unknown metrics in thresholds are failures too (typo guard)."""
    by = {s.metric: s for s in scores}
    failures = []
    for name, minimum in thresholds.minimums.items():
        if not thresholds.known(name):
            failures.append(f"{name}: not a known metric")
            continue
        s = by.get(name)
        if s is None:
            failures.append(f"{name}: no score produced")
        elif s.score < minimum:
            failures.append(f"{name}: {s.score:.2f} < {minimum:.2f} ({s.reason})")
    return failures


def online_metrics(names: list[str]) -> list[str]:
    """Reference-free subset safe for production scoring (Tool Correctness stays offline)."""
    return [n for n in names if CATALOGUE.get(n, ("", "", False))[2]]


def build_metric(name: str, threshold: float, model: Any, rubrics: dict[str, str] | None = None, options: dict[str, Any] | None = None) -> Any:
    """The DeepEval metric for a catalogue name or a rubric. `model` is a DeepEvalBaseLLM (see judge.py).
    Raises ValueError when the metric needs arguments `options` does not give."""
    if rubrics and name in rubrics:
        from deepeval.metrics import ConversationalGEval
        from deepeval.test_case import TurnParams

        return ConversationalGEval(name=name, criteria=rubrics[name], evaluation_params=[TurnParams.CONTENT], threshold=threshold, model=model)
    import deepeval.metrics as dm

    try:
        return getattr(dm, CATALOGUE[name][1])(threshold=threshold, model=model, **(options or {}))
    except TypeError as e:
        raise ValueError(f"{name}: needs options in thresholds.yaml ({e})") from e


def rule_metric(then: str, threshold: float, model: Any) -> Any:
    """Single-turn GEval: does the reply do what the matched rule says."""
    from deepeval.metrics import GEval
    from deepeval.test_case import SingleTurnParams

    return GEval(
        name=RULE_FOLLOWED,
        criteria=f"The assistant's reply to this message does what this rule asks: {then}",
        evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
        threshold=threshold,
        model=model,
    )


def ragas_metric(name: str, llm: Any) -> Any:
    """`ragas.<Metric>` → that Ragas collections metric. Raises ValueError for an unknown name."""
    import ragas.metrics.collections as rc

    cls = getattr(rc, name.removeprefix(RAGAS), None)
    if cls is None:
        raise ValueError(f"{name}: no such Ragas metric")
    try:
        return cls(llm=llm)
    except TypeError as e:  # needs more than a judge (embeddings, a reference)
        raise ValueError(f"{name}: needs more than a judge model ({e})") from e

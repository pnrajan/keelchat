"""Metric catalogue (DeepEval names), thresholds, and the CI gate."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

import yaml

#: metric name → (kind, deepeval class name, online-safe)
CATALOGUE: dict[str, tuple[str, str, bool]] = {
    "conversation_completeness": ("conversational", "ConversationCompletenessMetric", True),
    "knowledge_retention": ("conversational", "KnowledgeRetentionMetric", True),
    "role_adherence": ("conversational", "RoleAdherenceMetric", True),
    "conversation_relevancy": ("conversational", "ConversationRelevancyMetric", True),
    "faithfulness": ("single", "FaithfulnessMetric", True),
    "tool_correctness": ("single", "ToolCorrectnessMetric", False),
    "argument_correctness": ("single", "ArgumentCorrectnessMetric", False),
    "task_completion": ("single", "TaskCompletionMetric", True),
    "pii_leakage": ("single", "PIILeakageMetric", True),
    "non_advice": ("single", "NonAdviceMetric", True),
    "role_violation": ("single", "RoleViolationMetric", True),
    "no_language_switch_on_names": ("geval", "ConversationalGEval", True),
    "confirm_before_switching": ("geval", "ConversationalGEval", True),
    "job_text_as_posted": ("geval", "ConversationalGEval", True),
    "natural_tone": ("geval", "ConversationalGEval", True),
}

GEVAL_CRITERIA = {
    "no_language_switch_on_names": "The assistant never changes the conversation language because the user wrote a name or address in another language.",
    "confirm_before_switching": "Before switching language the assistant asks for confirmation and switches only after the user says yes.",
    "job_text_as_posted": "Job titles, locations and pay are shown exactly as the tool returned them (not translated), labeled as posted.",
    "natural_tone": (
        "The assistant sounds like a helpful person, not a form: it acknowledges what the user says, varies its wording, does not repeat "
        "boilerplate or echo values back field by field, asks one clear question at a time, never shows internal tags, ids or system "
        "terms, and does not ask for something the user already said."
    ),
}


@dataclass
class Thresholds:
    agent: str
    minimums: dict[str, float]
    calibration: dict[str, float] = dc_field(default_factory=dict)  # metric → human agreement recorded in docs/EVALS.md

    @classmethod
    def load(cls, path: str | Path) -> Thresholds:
        d = yaml.safe_load(Path(path).read_text())
        return cls(agent=d["agent"], minimums={k: float(v) for k, v in d["minimums"].items()}, calibration=dict(d.get("calibration", {})))


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
        if name not in CATALOGUE:
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


def build_metric(name: str, threshold: float, model: Any = None) -> Any:
    """Instantiate the DeepEval metric by catalogue name. `model` is a DeepEvalBaseLLM (see evals/judge.py)."""
    import deepeval.metrics as dm

    kind, cls_name, _ = CATALOGUE[name]
    if kind == "geval":
        from deepeval.metrics import ConversationalGEval
        from deepeval.test_case import TurnParams

        return ConversationalGEval(name=name, criteria=GEVAL_CRITERIA[name], evaluation_params=[TurnParams.CONTENT], threshold=threshold, model=model)
    cls = getattr(dm, cls_name)
    return cls(threshold=threshold, model=model) if model is not None else cls(threshold=threshold)

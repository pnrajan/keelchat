"""Online scoring: nightly sampling of live conversations (redacted), reference-free metrics, outcome-split signals."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from keelchat.capabilities.guardrails import PII_PATTERNS
from keelchat.state.models import SessionState


def redact(text: str) -> str:
    for cat, pat in PII_PATTERNS.items():
        text = re.sub(pat, f"[{cat}]", text, flags=re.IGNORECASE)
    return text


def redact_session(state: SessionState) -> dict[str, Any]:
    """What leaves production: redacted turns plus counters. Field values are never exported."""
    return {
        "id": state.id,
        "agent": state.agent,
        "policy_hash": state.policy_hash,
        "channel": state.channel,
        "language": state.language,
        "turns": [{"role": t.role, "text": redact(t.text), "step": t.step} for t in state.turns],
        "steps": {k: v.status for k, v in state.steps.items()},
        "digressions": state.digressions,
        "nudges": state.nudges,
        "reasks": dict(state.reasks),
        "exit_reason": state.exit_reason,
        "completed": state.completed,
        "submitted": "application" in state.outputs,
        "language_switches": sum(1 for t in state.turns if t.role == "agent" and "continuing in" in t.text.lower()),
    }


@dataclass
class OutcomeSplit:
    """Signals split by outcome (submitted vs not), sliced by agent, policy version, channel and language."""

    rows: list[dict[str, Any]]
    slices: tuple[str, ...] = ("agent", "policy_hash", "channel", "language")
    summary: dict[str, Any] = dc_field(default_factory=dict)

    def compute(self) -> dict[str, Any]:
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for r in self.rows:
            groups[tuple(r.get(s) for s in self.slices)].append(r)
        out: dict[str, Any] = {}
        for key, rows in groups.items():
            by_outcome: dict[str, Any] = {}
            for outcome in (True, False):
                sub = [r for r in rows if bool(r.get("submitted")) == outcome]
                n = len(sub) or 1
                by_outcome["submit" if outcome else "no_submit"] = {
                    "n": len(sub),
                    "digressions_per_conv": sum(r["digressions"] for r in sub) / n,
                    "digression_rate": sum(1 for r in sub if r["digressions"]) / n,
                    "failed_answers_per_field": self._reasks_per_field(sub),
                    "language_switches": sum(r.get("language_switches", 0) for r in sub),
                    "all_steps_complete": sum(1 for r in sub if r["completed"]) / n,
                    "early_exits": self._exits(sub),
                }
            out["|".join(str(k) for k in key)] = by_outcome
        with_d = [r for r in self.rows if r["digressions"]]
        without = [r for r in self.rows if not r["digressions"]]
        self.summary = {
            "digression_lift": (self._rate(with_d) - self._rate(without)) if with_d and without else None,
            "dropoff_after_failed_answer": {f: self._dropoff(f) for f in self._fields()},
            "wrong_exits": sum(1 for r in self.rows if r.get("exit_reason") == "disengaged" and r.get("returned_and_submitted")),
            "groups": out,
        }
        return self.summary

    def _fields(self) -> set[str]:
        return {f for r in self.rows for f in r.get("reasks", {})}

    @staticmethod
    def _rate(rows: list[dict[str, Any]]) -> float:
        return sum(1 for r in rows if r.get("submitted")) / (len(rows) or 1)

    def _dropoff(self, field: str) -> float:
        hit = [r for r in self.rows if r.get("reasks", {}).get(field)]
        return 1 - self._rate(hit) if hit else 0.0

    @staticmethod
    def _reasks_per_field(rows: list[dict[str, Any]]) -> dict[str, float]:
        acc: dict[str, float] = defaultdict(float)
        for r in rows:
            for f, n in r.get("reasks", {}).items():
                acc[f] += n
        return {f: v / (len(rows) or 1) for f, v in acc.items()}

    @staticmethod
    def _exits(rows: list[dict[str, Any]]) -> dict[str, int]:
        acc: dict[str, int] = defaultdict(int)
        for r in rows:
            if r.get("exit_reason") in ("max_reasks", "disengaged"):
                acc[r["exit_reason"]] += 1
        return dict(acc)


async def nightly_sample(sessions: list[SessionState], limit: int = 200) -> list[dict[str, Any]]:
    """Sample, redact and summarise live sessions; the caller scores them with `evals.metrics.online_metrics`."""
    return [redact_session(s) for s in sessions[:limit]]


def export_replay(state: SessionState, name: str | None = None) -> dict[str, Any]:
    """A failed live conversation as a new replay case (redacted user turns only)."""
    return {
        "name": name or f"live-{state.id}",
        "channel": state.channel,
        "messages": [redact(t.text) for t in state.turns if t.role == "user"],
        "expect": {"exit_reason": "completed" if state.completed else state.exit_reason},
    }

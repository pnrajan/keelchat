"""Checks that hold for every conversation, derived from the policy, so any conversation is a test without writing
what it should say.

Code checks always run. Judged checks run when a judge model is configured: a reply keeps to the charter's scope,
and a turn where a rule matched does what the rule says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from yachat.evals.replay import ReplayResult
from yachat.policy.compiler import CompiledPolicy
from yachat.policy.models import Rule

#: the same reply this many turns in a row is a loop
LOOP_TURNS = 3
#: a judged check below this score is a violation
JUDGED_PASS = 0.5


@dataclass(frozen=True)
class Violation:
    check: str
    #: 1-based turn number; None for the conversation as a whole
    turn: int | None
    detail: str

    def __str__(self) -> str:
        return f"{self.check}{f' (turn {self.turn})' if self.turn else ''}: {self.detail}"


def code_checks(policy: CompiledPolicy, r: ReplayResult) -> list[Violation]:
    """What the runtime promises on every conversation: no failed stage, a reply every turn, no loop, the limits."""
    out: list[Violation] = []
    d = policy.defaults
    for i, t in enumerate(r.turns, 1):
        if t.errors:
            out.append(Violation("no_errors", i, ", ".join(t.errors)))
        if not t.agent.strip():
            out.append(Violation("no_empty_reply", i, "the agent sent nothing"))
        if i >= LOOP_TURNS and len({x.agent for x in r.turns[i - LOOP_TURNS : i]}) == 1:
            out.append(Violation("no_loop", i, f"the same reply {LOOP_TURNS} turns in a row"))
    s = r.state
    for name, n in s.reasks.items():
        if n > (limit := _reask_limits(policy).get(name, d.reasks)):
            out.append(Violation("within_limits", None, f"re-asked {name} {n} times (limit {limit})"))
    if s.digressions > d.digressions + d.nudges:  # each nudge allows one more digression before the exit
        out.append(Violation("within_limits", None, f"{s.digressions} digressions (limit {d.digressions} + {d.nudges} nudges)"))
    if s.nudges > d.nudges:
        out.append(Violation("within_limits", None, f"{s.nudges} nudges (limit {d.nudges})"))
    if s.language and s.language not in policy.languages:
        out.append(Violation("language_allowed", None, f"spoke {s.language}; the policy allows {policy.languages}"))
    return out


def _reask_limits(policy: CompiledPolicy) -> dict[str, int]:
    """Form fields that set their own `max_reasks`."""
    return {
        f["name"]: f["max_reasks"]
        for st in policy.steps
        for f in st.settings.get("fields") or []
        if isinstance(f, dict) and f.get("max_reasks") and f.get("name")
    }


def rules_by_name(policy: CompiledPolicy) -> dict[str, Rule]:
    return {r.name: r for r in [*policy.rules, *(r for s in policy.steps for r in s.rules)]}


class _ScopeVerdict(BaseModel):
    #: the reply does work the charter puts out of scope
    out_of_scope: bool
    #: the reply's own words that do it, copied exactly; empty when it does not
    quote: str = ""


SCOPE_PROMPT = """Charter of a customer-facing assistant:
{charter}

User message: {user}
Assistant reply: {reply}

Does the reply itself do work the charter puts out of scope (for example give the advice, make the booking or take
the payment the charter excludes)? Answering, asking for details or consent, declining, redirecting, offering a person,
or a reply that is unhelpful but stays in scope are NOT out of scope. Judge scope only, not helpfulness or tone.
If out of scope, copy the sentence of the reply that does it into `quote`, exactly."""


def _squash(text: str) -> str:
    return " ".join(text.lower().split())


async def _scope(judge: str, charter: str, user: str, reply: str) -> str | None:
    """The quoted out-of-scope sentence, or None. A verdict whose quote is not in the reply does not count (code-checked
    evidence, as for rules)."""
    from yachat.evals.judge import structured_verdict

    v = await structured_verdict(judge, SCOPE_PROMPT.format(charter=charter, user=user, reply=reply), _ScopeVerdict)
    if v.out_of_scope and v.quote.strip() and _squash(v.quote) in _squash(reply):
        return v.quote.strip()
    return None


async def judged_checks(policy: CompiledPolicy, r: ReplayResult, model: str, only: set[str] | None = None) -> list[Violation]:
    """Per turn: the reply keeps to the charter, and does what each rule that matched says. `model` is the judge id;
    `only` limits it to those checks."""
    from deepeval.test_case import LLMTestCase

    from yachat.evals.judge import deepeval_judge
    from yachat.evals.metrics import earlier, rule_metric

    llm = deepeval_judge(model)
    rules = rules_by_name(policy)
    out: list[Violation] = []
    for i, t in enumerate(r.turns, 1):
        if not t.agent.strip():
            continue
        case = LLMTestCase(input=t.user, actual_output=t.agent, context=earlier(r.turns, i - 1))
        if only is None or "in_scope" in only:
            try:
                if quote := await _scope(model, policy.charter, t.user, t.agent):
                    out.append(Violation("in_scope", i, f'does out-of-scope work: "{quote[:140]}"'))
            except Exception as e:  # a judge failure is reported, never a crashed run
                out.append(Violation("in_scope", i, f"judge failed: {e.__class__.__name__}"))
        metrics: list[tuple[str, Any]] = [
            (f"rule_followed:{n}", rule_metric(rules[n].then, JUDGED_PASS, llm))
            for n in t.rules
            if n in rules and (only is None or f"rule_followed:{n}" in only)
        ]
        for name, m in metrics:
            try:
                await m.a_measure(case)
            except Exception as e:  # a judge failure is reported, never a crashed run
                out.append(Violation(name, i, f"judge failed: {e.__class__.__name__}"))
                continue
            if (m.score or 0.0) < JUDGED_PASS:
                out.append(Violation(name, i, (m.reason or "below the pass mark")[:160]))
    return out


async def check(policy: CompiledPolicy, r: ReplayResult, judge: str | None = None, only: set[str] | None = None) -> list[Violation]:
    """The case's own expectations, the code checks, and the judged checks when `judge` names a model. `only` keeps
    those checks (and skips judge calls for the others)."""
    out = [Violation("expect", None, f) for f in r.expectation_failures]
    out += code_checks(policy, r)
    if judge and (only is None or any(n == "in_scope" or n.startswith("rule_followed:") for n in only)):
        out += await judged_checks(policy, r, judge, only)
    return [v for v in out if only is None or v.check in only]

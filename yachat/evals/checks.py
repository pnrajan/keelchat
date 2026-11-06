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
    evidence)."""
    from yachat.evals.judge import structured_verdict

    v = await structured_verdict(judge, SCOPE_PROMPT.format(charter=charter, user=user, reply=reply), _ScopeVerdict)
    if v.out_of_scope and v.quote.strip() and _squash(v.quote) in _squash(reply):
        return v.quote.strip()
    return None


class _RulePart(BaseModel):
    #: one thing the rule's `then` asks for
    asks: str
    #: false only when it depends on a condition the conversation shows does not hold
    applies: bool
    #: the reply itself does exactly this
    done: bool
    #: the reply's words that do it, copied exactly; empty when not done
    quote: str = ""


class _RuleVerdict(BaseModel):
    parts: list[_RulePart]


RULE_PROMPT = """A customer-facing assistant has this rule for the current message:
When: {when}
Then the reply should: {then}

Conversation so far:
{context}

User message: {user}
Assistant reply: {reply}

Split "Then" into each separate thing it asks for, and judge each one:
- applies: false only when it depends on a condition (such as "if it is not known yet") that the conversation so far
  shows does not hold; otherwise true.
- done: true only when the reply itself does exactly that thing. Something similar but different does not count: a
  refund is not a replacement, asking for one detail is not asking for another, a link is not an offer.
- quote: when done, the words of the reply that do it, copied exactly.
Ignore everything the rule does not ask for: other questions, extra information, tone, length."""


async def _rule(judge: str, rule: Rule, user: str, reply: str, context: list[str]) -> str | None:
    """What the reply leaves out of the rule, or None when it does every part that applies. The judge only splits the
    rule and reads the reply; code decides, and a part counts as done only when its quote is in the reply."""
    from yachat.evals.judge import structured_verdict

    prompt = RULE_PROMPT.format(when=rule.when, then=rule.then, context="\n\n".join(context), user=user, reply=reply)
    v = await structured_verdict(judge, prompt, _RuleVerdict)
    if not v.parts:
        return "the judge found nothing the rule asks for"
    missing = [p.asks for p in v.parts if p.applies and not (p.done and p.quote.strip() and _squash(p.quote) in _squash(reply))]
    return f"does not: {'; '.join(missing)}" if missing else None


async def rule_followed(judge: str, rule: Rule, turns: list[Any], i: int) -> tuple[float, str]:
    """`rule_followed` as a metric score for turn `i` (0-based): 1.0 or 0.0, and what was missing."""
    from yachat.evals.metrics import earlier

    t = turns[i]
    missing = await _rule(judge, rule, t.user, t.agent, earlier(turns, i))
    return (0.0, missing) if missing else (1.0, "")


async def judged_checks(policy: CompiledPolicy, r: ReplayResult, model: str, only: set[str] | None = None, turn: int | None = None) -> list[Violation]:
    """Per turn: the reply keeps to the charter, and does what each rule that matched says. `model` is the judge id;
    `only` limits it to those checks, `turn` (from 1) to that turn (the earlier ones are its context)."""
    rules = rules_by_name(policy)
    out: list[Violation] = []
    for i, t in enumerate(r.turns, 1):
        if not t.agent.strip() or (turn is not None and i != turn):
            continue
        if only is None or "in_scope" in only:
            try:
                if quote := await _scope(model, policy.charter, t.user, t.agent):
                    out.append(Violation("in_scope", i, f'does out-of-scope work: "{quote[:140]}"'))
            except Exception as e:  # a judge failure is reported, never a crashed run
                out.append(Violation("in_scope", i, f"judge failed: {e.__class__.__name__}"))
        for n in t.rules:
            name = f"rule_followed:{n}"
            if n not in rules or (only is not None and name not in only):
                continue
            try:
                score, missing = await rule_followed(model, rules[n], r.turns, i - 1)
            except Exception as e:  # a judge failure is reported, never a crashed run
                out.append(Violation(name, i, f"judge failed: {e.__class__.__name__}"))
                continue
            if score < JUDGED_PASS:
                out.append(Violation(name, i, missing[:160]))
    return out


async def check(policy: CompiledPolicy, r: ReplayResult, judge: str | None = None, only: set[str] | None = None) -> list[Violation]:
    """The case's own expectations, the code checks, and the judged checks when `judge` names a model. `only` keeps
    those checks (and skips judge calls for the others)."""
    out = [Violation("expect", None, f) for f in r.expectation_failures]
    out += code_checks(policy, r)
    if judge and (only is None or any(n == "in_scope" or n.startswith("rule_followed:") for n in only)):
        out += await judged_checks(policy, r, judge, only)
    return [v for v in out if only is None or v.check in only]

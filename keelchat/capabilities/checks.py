"""Built-in checks: code reads every draft before a model verifier does. Deterministic, side-effect free; a rejection
goes back to the planner or the Rewriter in the verifier's own terms. All registered checks run on every draft."""

from __future__ import annotations

import re

from keelchat.registry.core import Evidence, Finding, check
from keelchat.runtime.respond.rewriter import facts

#: a model deliberating instead of replying: seen live from a model told not to reason ("The user wants to know...")
LEAK = re.compile(
    r"\b(the user (wants|is asking|asked|needs|says|mentioned)|looking at the (tool|passage|context|output|search)"
    r"|according to the tool output|i should (say|state|answer|respond|mention|tell)|let me (check|look|think|see)"
    r"|i need to (check|answer|respond|find))\b",
    re.IGNORECASE,
)
#: the pipeline's own words
INTERNAL = ("plain version", "tool output", "the verifier", "system prompt", "verbatim")


@check("no_reasoning_leak")
def no_reasoning_leak(draft: str, ev: Evidence) -> Finding | None:
    """The draft talks to the user, not about them: no model deliberation ("The user wants...", "I should say...")."""
    m = LEAK.search(draft)
    return Finding("incomplete", "a reply written to the user, not reasoning about the user", f"reads like reasoning: {m.group(0)!r}") if m else None


@check("no_internal_names")
def no_internal_names(draft: str, ev: Evidence) -> Finding | None:
    """No internal names reach the user: tool and step names with an underscore, and the pipeline's own terms."""
    low = draft.lower()
    hit = next((n for n in sorted(ev.names) if "_" in n and n.lower() in low), None) or next((p for p in INTERNAL if p in low), None)
    return Finding("incomplete", "no internal names in the reply", f"mentions {hit!r}") if hit else None


def _plain(token: str) -> str:
    return token.strip("*_`.").replace(",", "")


@check("numbers_grounded")
def numbers_grounded(draft: str, ev: Evidence) -> Finding | None:
    """Every number, id, amount, date or code in the draft appears in a source: a tool output, the plain version or the
    user's own words. Bare one- or two-digit integers (list markers, ordinals) are not held to it."""
    have = {_plain(f) for src in ev.sources for f in facts(src)}
    loose = sorted({_plain(f) for f in facts(draft)} - have - {""})
    loose = [f for f in loose if not (f.isdigit() and len(f) <= 2)]
    return Finding("calculation_error", "only figures the sources give", f"not in any source: {loose}", tuple(loose)) if loose else None


@check("question_kept")
def question_kept(draft: str, ev: Evidence) -> Finding | None:
    """When the step still needs an answer, the reply asks for it."""
    return Finding("incomplete", f"the question still to ask: {ev.asked}", "no question in the reply") if ev.asked and "?" not in draft else None

"""Default guardrails: input (prompt_injection, sql_injection) and output (pii, profanity)."""

from __future__ import annotations

import re
from typing import ClassVar

from yachat.registry.core import GuardContext, Guardrail, GuardResult, guardrail


class _PatternGuard(Guardrail):
    patterns: ClassVar[tuple[str, ...]] = ()
    label: ClassVar[str] = ""

    async def check(self, text: str, ctx: GuardContext) -> GuardResult:
        for pat in self.patterns:
            if re.search(pat, text, re.IGNORECASE):
                return GuardResult(flagged=True, reason=f"{self.label}: matched {pat!r}")
        return GuardResult()


@guardrail("prompt_injection", stage="input")
class PromptInjection(_PatternGuard):
    """Blocks attempts to override the agent's instructions."""

    label = "prompt_injection"
    patterns = (
        r"ignore (all |the )?(previous|prior|above) instructions",
        r"disregard (your|the) (instructions|system prompt)",
        r"reveal (your|the) system prompt",
        r"you are now (a|an) ",
        r"<\s*/?\s*system\s*>",
    )


@guardrail("sql_injection", stage="input")
class SqlInjection(_PatternGuard):
    """Blocks SQL injection payloads."""

    label = "sql_injection"
    patterns = (r"'\s*or\s+'?1'?\s*=\s*'?1", r";\s*drop\s+table", r"union\s+(all\s+)?select", r"--\s*$")


PII_PATTERNS: dict[str, str] = {
    "email": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "phone": r"(?<!\d)(?:\+?1[ -.]?)?\(?\d{3}\)?[ -.]?\d{3}[ -.]?\d{4}(?!\d)",
    "ssn": r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)",
    "address": r"\b\d{1,6}\s+[A-Za-z0-9.'\- ]{2,40}\b(street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|court|ct)\b\.?",
}


@guardrail("pii", stage="output")
class Pii(Guardrail):
    """Redacts leaked PII unless the current step asked for that category (e.g. address in FormFill)."""

    async def check(self, text: str, ctx: GuardContext) -> GuardResult:
        out, hits = text, []
        for cat, pat in PII_PATTERNS.items():
            if cat in ctx.allowed_pii:
                continue
            if re.search(pat, out, re.IGNORECASE):
                hits.append(cat)
                out = re.sub(pat, f"[{cat} redacted]", out, flags=re.IGNORECASE)
        return GuardResult(flagged=True, reason=f"pii: {', '.join(hits)}", redacted=out) if hits else GuardResult()


@guardrail("profanity", stage="output")
class Profanity(Guardrail):
    """Masks profanity in replies."""

    words: ClassVar[tuple[str, ...]] = ("damn", "crap", "shit", "fuck", "bastard")

    async def check(self, text: str, ctx: GuardContext) -> GuardResult:
        pat = r"\b(" + "|".join(re.escape(w) for w in self.words) + r")\b"
        if re.search(pat, text, re.IGNORECASE):
            return GuardResult(flagged=True, reason="profanity", redacted=re.sub(pat, "***", text, flags=re.IGNORECASE))
        return GuardResult()


@guardrail("no_legal_advice", stage="output")
class NoLegalAdvice(_PatternGuard):
    """Blocks replies that give legal advice (what the charter rules out of scope)."""

    label = "no_legal_advice"
    patterns = (
        r"\byou (should|could|can) (sue|file a lawsuit|take legal action)\b",
        r"\b(my|this is) legal advice\b",
        r"\byour (visa|immigration) (status|case) (will|should)\b",
        r"\blegally,? you (must|should|are entitled)\b",
    )

    async def check(self, text: str, ctx: GuardContext) -> GuardResult:
        r = await super().check(text, ctx)
        if r.flagged:
            r.redacted = "I can't give legal advice; a qualified professional can help with that."
        return r


@guardrail("no_medical_advice", stage="output")
class NoMedicalAdvice(_PatternGuard):
    """Blocks replies that give medical advice."""

    label = "no_medical_advice"
    patterns = (
        r"\byou (should|could) (take|stop taking|increase|reduce) (your )?(medication|dose|pills)\b",
        r"\b(you have|sounds like) (a|an) (infection|disorder|condition)\b",
        r"\bmy diagnosis\b",
        r"\bmedically,? you should\b",
    )

    async def check(self, text: str, ctx: GuardContext) -> GuardResult:
        r = await super().check(text, ctx)
        if r.flagged:
            r.redacted = "I can't give medical advice; please talk to a healthcare professional."
        return r

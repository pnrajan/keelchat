"""PII redaction for logs and exports. Field values marked sensitive are never logged at all."""

from __future__ import annotations

import re

from yachat.capabilities.guardrails import PII_PATTERNS


def redact_pii(text: str) -> str:
    for cat, pat in PII_PATTERNS.items():
        text = re.sub(pat, f"[{cat}]", text, flags=re.IGNORECASE)
    return text

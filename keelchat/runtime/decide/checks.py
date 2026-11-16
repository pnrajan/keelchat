"""Code checks on a draft before any model verdict (law 5: code decides). Every registered `@check` runs on every
draft the planner writes and every rewrite the Rewriter makes; the first rejection goes back in the verifier's own
terms, and the model verifier is not asked that round."""

from __future__ import annotations

from keelchat.logging import get_logger
from keelchat.policy.compiler import CompiledPolicy
from keelchat.registry.core import REGISTRY, Evidence, Finding

log = get_logger("checks")


def run_checks(draft: str, evidence: Evidence) -> tuple[str, Finding] | None:
    """The first check that rejects the draft, by name. A check that raises is logged and skipped; the others still run."""
    for spec in REGISTRY.checks():
        try:
            finding = spec.fn(draft, evidence)
        except Exception as e:  # a broken check must not take the turn down, nor pass for the others
            log.error("check failed to run", check=spec.name, error=f"{e.__class__.__name__}: {e}")
            continue
        if finding is not None:
            return spec.name, finding
    return None


def internal_names(policy: CompiledPolicy) -> set[str]:
    """Names a user should never read: the policy's tools and steps."""
    return set(policy.allowed_tools) | {s.name for s in policy.steps}

"""Output guardrails compose: every flagged check's redaction survives, a block is final, and a guardrail that is
missing or broken cannot crash the turn or undo another's work. No model calls."""

from __future__ import annotations

from keelchat.policy.compiler import build
from keelchat.registry.core import NodeResult
from keelchat.runtime.context import InboundMessage, Services, TurnContext
from keelchat.runtime.llm import ModelConfig
from keelchat.runtime.respond.guardrails import BLOCKED, OutputGuardrails
from keelchat.state.models import SessionState
from tests.helpers import RecordingLLM

POLICY = "agent: a\ncharter: c\nguardrails: {output: [pii, profanity, no_legal_advice]}\n"


def _ctx(policy_yaml: str, reply: str, output: list[str] | None = None) -> TurnContext:
    p = build(policy_yaml)
    if output is not None:  # a name that drifted out of the registry after the build
        p = p.model_copy(update={"defaults": p.defaults.model_copy(update={"output_guardrails": output})})
    ctx = TurnContext(
        SessionState(user_id="u", agent=p.agent, policy_hash=p.hash, channel="web"),
        p,
        InboundMessage("hi", "web", "u"),
        Services(RecordingLLM(), ModelConfig.from_env(), None, None),  # type: ignore[arg-type]
    )
    ctx.node_result = NodeResult(reply)
    return ctx


async def test_two_flagged_guardrails_both_redact() -> None:
    ctx = _ctx(POLICY, "Damn, email hr-lead@corp.com about it.")
    await OutputGuardrails().run(ctx)
    assert ctx.node_result is not None
    out = ctx.node_result.text
    assert "hr-lead@corp.com" not in out and "[email redacted]" in out, out
    assert "Damn" not in out and "***" in out, out


async def test_a_block_is_final_even_when_a_later_guardrail_only_redacts() -> None:
    ctx = _ctx(POLICY, "Legally, you must sue them; call 555-123-4567.")
    await OutputGuardrails().run(ctx)
    assert ctx.node_result is not None
    out = ctx.node_result.text
    assert out == "I can't give legal advice; a qualified professional can help with that." or out == BLOCKED["en"], out
    assert "555-123-4567" not in out


async def test_a_guardrail_that_is_not_registered_fails_open_without_crashing() -> None:
    ctx = _ctx(POLICY, "Email hr-lead@corp.com.", output=["pii", "no_such_guardrail"])
    await OutputGuardrails().run(ctx)
    assert ctx.node_result is not None and "[email redacted]" in ctx.node_result.text
    assert any("no_such_guardrail" in e for e in ctx.errors)

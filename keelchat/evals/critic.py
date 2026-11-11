"""A stronger model judges one trial conversation against its policy and says what to fix. Fixes the policy can make
go back to the drafter (`edit_policy`); fixes only code can make become `FixTask`s for a developer or Claude Code.
The model judges; code checks what it names (turns, kinds, counts)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, Field

from keelchat.datasets.models import DatasetKind
from keelchat.policy.drafter import catalog
from keelchat.runtime.llm import LLMClient, LLMRequest, ModelConfig, untrusted
from keelchat.state.models import FixTask, ReviewTurn

#: most problems and fixes one judgement returns
FINDINGS_LIMIT = 10
FIXES_LIMIT = 6

#: where code lives, so a code fix can point at files (a hint: Claude Code reads the repo before changing it)
CODE_MAP = """\
keelchat/policy/ (models, compiler, schema): new policy keys or step settings
keelchat/capabilities/nodes/: flow steps (form_fill, knowledge, survey, handoff, ...)
keelchat/capabilities/tools.py, guardrails.py, preprocessors/: tools, guardrails, message pre-processing
keelchat/runtime/decide/: intent_detector (answers, digressions, small talk), orchestrator (which step next), planner
keelchat/runtime/respond/: rewriter (reply wording, plan, verifier), guardrails, response_generator
keelchat/channels/: channel limits and formatting
tests/: one file per area; tests call the real model"""

SYSTEM = f"""You review one conversation between a user and a chat agent, against the agent's policy (YAML).
Be strict and concrete: name the turn, what the agent did, and what it should have done. Praise nothing.

Then propose fixes. Each fix is one of:
- "policy": the YAML can make it (charter wording, a rule, a field's ask/expect, a step setting, limits, guardrails,
  small_talk, handoff). Say the change in plain words, as an owner would ask for it.
- "code": the YAML cannot express it (a missing step type, tool or guardrail; the runtime misread an answer, ignored
  a rule, or broke a promise the policy makes). Say what to change, the likely files, and the behaviour a test should
  check that fails today.
Prefer "policy" when the policy can fix it. Only propose fixes for problems you found. The catalog lists what a
policy can name today.

Where code lives:
{CODE_MAP}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "one or two sentences: how the conversation went"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "turn": {"type": "integer", "description": "1-based turn number"},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "problem": {"type": "string"},
                },
                "required": ["turn", "severity", "problem"],
            },
        },
        "fixes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["policy", "code"]},
                    "title": {"type": "string", "description": "a short imperative line"},
                    "change": {"type": "string"},
                    "turns": {"type": "array", "items": {"type": "integer"}},
                    "files": {"type": "array", "items": {"type": "string"}, "description": "code fixes: likely files"},
                    "test": {"type": "string", "description": "code fixes: behaviour a test should check"},
                },
                "required": ["kind", "title", "change", "turns", "files", "test"],
            },
        },
    },
    "required": ["summary", "findings", "fixes"],
}


class Finding(BaseModel):
    turn: int
    severity: Literal["high", "medium", "low"]
    problem: str


class Fix(BaseModel):
    kind: Literal["policy", "code"]
    title: str
    change: str
    turns: list[int] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    test: str = ""


class Critique(BaseModel):
    model: str
    summary: str
    findings: list[Finding]
    fixes: list[Fix]


async def critique(llm: LLMClient, models: ModelConfig, yaml: str, turns: list[ReviewTurn], tenant: str, datasets: Mapping[str, DatasetKind]) -> Critique:
    """The critic model's verdict on `turns` under the policy `yaml`. Needs `models.critic`."""
    if not models.critic:
        raise ValueError("no critic model: set KEELCHAT_MODEL_CRITIC")
    convo = "\n".join(
        f"Turn {i}\nUser: {t.user}\nAgent: {t.agent}" + (f"\n(rules matched: {', '.join(t.rules)})" if t.rules else "") for i, t in enumerate(turns, 1)
    )
    content = f"Catalog:\n{catalog(tenant, datasets)}\n\n" + untrusted("policy", yaml) + "\n\n" + untrusted("conversation", convo)
    req = LLMRequest("critic", models.critic, SYSTEM, [{"role": "user", "content": content}], max_tokens=4000, effort="high", timeout_s=180)
    raw = await llm.structured(req, SCHEMA)
    return _checked(models.critic, raw, len(turns))


def _checked(model: str, raw: Mapping[str, object], n: int) -> Critique:
    """Only what names a real turn is kept; a fix with no real turn is dropped; counts are capped."""

    def real(t: object) -> bool:
        return isinstance(t, int) and 1 <= t <= n

    findings = [Finding.model_validate(f) for f in _items(raw, "findings")]
    findings = [f for f in findings if real(f.turn)]
    findings.sort(key=lambda f: ("high", "medium", "low").index(f.severity))
    fixes = []
    for f in (Fix.model_validate(x) for x in _items(raw, "fixes")):
        f.turns = sorted({t for t in f.turns if real(t)})
        if f.turns and f.title.strip() and f.change.strip():
            fixes.append(f if f.kind == "code" else f.model_copy(update={"files": [], "test": ""}))
    return Critique(model=model, summary=str(raw.get("summary", "")), findings=findings[:FINDINGS_LIMIT], fixes=fixes[:FIXES_LIMIT])


def _items(raw: Mapping[str, object], key: str) -> list[dict[str, object]]:
    v = raw.get(key)
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def policy_request(fixes: list[Fix]) -> str:
    """Policy fixes as one plain-words request for `edit_policy`."""
    return "\n".join(f"- {f.title}: {f.change}" for f in fixes)


def issue_markdown(task: FixTask) -> str:
    """The task as a GitHub issue body."""
    convo = "\n".join(f"> **User:** {t.user}\n>\n> **Agent:** {t.agent}\n>" for t in task.turns).rstrip(">\n")
    files = "\n".join(f"- `{f}`" for f in task.files) or "- (not named)"
    return (
        f"## Problem\n\n{task.problem}\n\n## Change\n\n{task.change}\n\n"
        + (f"## Test (fails before the fix)\n\n{task.test}\n\n" if task.test else "")
        + f"## Likely files\n\n{files}\n\n## Conversation (agent `{task.agent}`, policy `{task.policy_hash or 'unsaved'}`)\n\n{convo}\n\n"
        f"---\nkeelchat fix task `{task.id}`\n"
    )

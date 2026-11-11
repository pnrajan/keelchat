"""Readiness: what an agent still needs before it can launch, as a checklist the console shows next to the editor.

Every check is computed in code from the policy, the registry and this deployment's configuration; nothing is asked of
a model. Each item names the area it belongs to and, when the console can do it, a `fix` action:
`save`, `charter`, `add_step:<node>`, `guardrails`, `knowledge`, `dataset`, `tests`."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from keelchat.datasets.models import DatasetKind
from keelchat.policy.compiler import CompiledPolicy, build
from keelchat.policy.errors import PolicyValidationError
from keelchat.policy.templates import BLANK
from keelchat.registry.core import REGISTRY
from keelchat.runtime.llm_factory import TOOL_BACKENDS
from keelchat.state.store import AgentTestStore, AgentVersionStore

Status = Literal["ok", "warning", "missing"]
Area = Literal["policy", "connections", "safety", "launch"]
EVALS = Path(__file__).resolve().parents[2] / "evals"
BLANK_CHARTER = "Say what the bot helps with, its tone, and what is out of scope."
assert BLANK_CHARTER in BLANK


class Item(BaseModel):
    id: str
    area: Area
    status: Status
    title: str
    detail: str = ""
    #: a missing blocking item stops a launch; the rest are advice
    blocking: bool = False
    fix: str | None = None


class Report(BaseModel):
    agent: str | None
    items: list[Item]
    #: 0-100: share of checks that are ok (warnings count half)
    score: int
    #: nothing blocking is missing
    ready: bool


def _tool_items(policy: CompiledPolicy, impls: set[str], datasets: Mapping[str, DatasetKind]) -> list[Item]:
    out = []
    for name in policy.allowed_tools:
        env = TOOL_BACKENDS.get(name)
        spec = REGISTRY.tool(name, policy.tenant) if REGISTRY.has_tool(name, policy.tenant) else None
        label = (spec.description.split("\n")[0].rstrip(".") if spec else name) or name
        if env is None or os.getenv(env):
            out.append(Item(id=f"tool:{name}", area="connections", status="ok", title=f"{name} is connected", detail=label))
        elif name == "knowledge_search" and (kbs := ", ".join(d for d in policy.datasets if datasets.get(d) == "unstructured")):
            # its knowledge bases are searched for real (built into the server, or the Knowledge Service), never sample data
            out.append(Item(id=f"tool:{name}", area="connections", status="ok", title="Answers from your knowledge base", detail=kbs))
        elif name in impls:
            out.append(
                Item(
                    id=f"tool:{name}",
                    area="connections",
                    status="warning",
                    title=f"{name} uses sample data",
                    detail=f"Answers come from built-in sample data. Set {env} to connect the real service before launch.",
                    fix="knowledge" if name == "knowledge_search" else None,
                )
            )
        else:
            out.append(
                Item(
                    id=f"tool:{name}",
                    area="connections",
                    status="missing",
                    blocking=True,
                    title=f"{name} is not connected",
                    detail=f"The agent will say it is unavailable. Set {env}, or run with sample data while you build.",
                    fix="knowledge" if name == "knowledge_search" else None,
                )
            )
    return out


def _policy_items(policy: CompiledPolicy) -> list[Item]:
    out = []
    charter = policy.charter.lower()
    scoped = any(w in charter for w in ("out of scope", "won't", "do not", "don't", "never", "only "))
    if policy.charter.strip() == BLANK_CHARTER:
        out.append(
            Item(
                id="scope",
                area="policy",
                status="missing",
                blocking=True,
                title="The charter is still the template text",
                detail="Say what the agent helps with, its tone, and what it should decline.",
                fix="charter",
            )
        )
        return out + _structure_items(policy)
    out.append(
        Item(id="scope", area="policy", status="ok", title="The charter says what is out of scope")
        if scoped
        else Item(
            id="scope",
            area="policy",
            status="warning",
            title="The charter does not say what is out of scope",
            detail='Add a sentence like "Out of scope: legal advice, other stores." so the agent declines the rest.',
            fix="charter",
        )
    )
    return out + _structure_items(policy)


def _structure_items(policy: CompiledPolicy) -> list[Item]:
    out = []
    steps = [s.node for s in policy.steps]
    memory = policy.memory
    if memory and "greet_consent" not in steps:
        out.append(
            Item(
                id="consent",
                area="safety",
                status="missing",
                blocking=True,
                title="It remembers answers but never asks for consent",
                detail=f"Memory keeps {', '.join(m.name for m in memory)}; add the consent step first.",
                fix="add_step:greet_consent",
            )
        )
    elif memory and steps.index("greet_consent") > 0:
        out.append(
            Item(
                id="consent",
                area="safety",
                status="missing",
                blocking=True,
                title="Consent is asked after other steps",
                detail=f"{', '.join(steps[: steps.index('greet_consent')])} run before consent is given; move the consent step first.",
                fix="add_step:greet_consent",
            )
        )
    elif memory:
        out.append(Item(id="consent", area="safety", status="ok", title="Consent is asked before anything is remembered"))
    if not policy.has_flow:
        out.append(
            Item(
                id="flow",
                area="policy",
                status="warning",
                title="No steps: the agent improvises every turn",
                detail="Fine for open conversations. Add steps when there is a task to finish (a form, a lookup).",
                fix="add_step",
            )
        )
    extra_in = [g for g in policy.defaults.input_guardrails if g not in policy.defaults.base_input_guardrails]
    out.append(
        Item(id="guardrails", area="safety", status="ok", title="Input checks beyond the defaults are on", detail=", ".join(extra_in))
        if extra_in
        else Item(
            id="guardrails",
            area="safety",
            status="warning",
            title="Only the default safety checks run",
            detail="Consider sql_injection on input and no_legal_advice or profanity on output.",
            fix="guardrails",
        )
    )
    unchecked = [s.name for s in policy.steps if not s.verify]
    if unchecked:
        out.append(
            Item(
                id="verify",
                area="safety",
                status="warning",
                title="Some steps send replies unchecked",
                detail=f"verify is off for {', '.join(unchecked)}.",
            )
        )
    return out


async def readiness(
    yaml: str, tenant: str, datasets: Mapping[str, DatasetKind], impls: set[str], versions: AgentVersionStore, tests: AgentTestStore | None = None
) -> Report:
    """`impls`: tools this runtime has an implementation for without a backend URL (sample data). `tests`: the tests
    kept with agents (the console's Test tab); an eval set under evals/ counts too."""
    items: list[Item] = []
    try:
        policy = build(yaml, tenant=tenant, datasets=datasets)
    except PolicyValidationError as e:
        detail = "; ".join(f"{x.path}: {x.message}" for x in e.errors[:5])
        items.append(Item(id="builds", area="policy", status="missing", blocking=True, title=f"{len(e.errors)} problem(s) in the policy", detail=detail))
        return _report(None, items)
    items.append(Item(id="builds", area="policy", status="ok", title="The policy builds"))
    items += _policy_items(policy)
    items += _tool_items(policy, impls, datasets)
    active = await versions.active(tenant, policy.agent)
    saved = await versions.versions(tenant, policy.agent)
    if active is not None and active.hash == policy.hash:
        items.append(Item(id="saved", area="launch", status="ok", title=f"Saved as v{active.version}, active"))
    elif saved:
        items.append(
            Item(id="saved", area="launch", status="warning", title="Edits not saved", detail="New sessions still use the active version.", fix="save")
        )
    else:
        items.append(
            Item(id="saved", area="launch", status="missing", blocking=True, title="Not saved yet", detail="Save a version so it can be used.", fix="save")
        )
    has_tests = any((EVALS / policy.agent.replace("-", "_") / f).is_file() for f in ("replay.yaml", "generated.yaml"))
    has_tests = has_tests or (tests is not None and bool(await tests.list(tenant, policy.agent)))
    items.append(
        Item(id="tests", area="launch", status="ok", title="Has test conversations")
        if has_tests
        else Item(
            id="tests",
            area="launch",
            status="missing",
            title="No test conversations",
            detail="Save conversations as tests, or draft them from the policy, in the Test tab, so changes are checked before they go live.",
            fix="tests",
        )
    )
    items.append(
        Item(
            id="channel",
            area="launch",
            status="warning",
            title="Not published to a channel yet",
            detail="Publish it as a website widget and link from Publish · results, or connect WhatsApp, SMS, Slack or phone from Channels.",
        )
    )
    return _report(policy.agent, items)


def _report(agent: str | None, items: list[Item]) -> Report:
    points = sum(1.0 if i.status == "ok" else 0.5 if i.status == "warning" else 0.0 for i in items)
    return Report(agent=agent, items=items, score=round(100 * points / max(1, len(items))), ready=not any(i.blocking and i.status == "missing" for i in items))

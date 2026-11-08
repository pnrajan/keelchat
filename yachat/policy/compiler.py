"""Compile: resolve defaults, allowed tools, step graph, manifest versions, content hash."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from yachat.datasets.models import DatasetKind, dataset_refs
from yachat.policy.defaults import DEFAULTS, Defaults
from yachat.policy.errors import PolicyValidationError
from yachat.policy.loader import load_policy
from yachat.policy.memory_types import type_to_schema
from yachat.policy.models import AgentPolicy, HandoffSpec, Mode, Rule, Tier
from yachat.policy.validator import resolve_repairs, resolved_requires, validate_policy
from yachat.registry.core import REGISTRY


class CompiledStep(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    node: str
    requires: list[str]
    mode: Mode
    settings: dict[str, Any]
    tools: list[str]
    produces: list[str]
    #: resolved repair attempts per stage (decorator/platform default, policy limits, step override)
    repairs: dict[str, int] = Field(default_factory=dict)
    #: phrased replies are checked before they are sent (node default, step override)
    verify: bool = True
    #: rules that apply while this step is asking
    rules: list[Rule] = Field(default_factory=list)
    #: the model tier this step's work runs on; None: each job keeps its usual tier
    model: Tier | None = None
    high_stakes: bool = False


class CompiledMemoryItem(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    extract: str | None
    from_: str | None
    schema_: dict[str, Any] = Field(alias="schema")
    half_life_s: float
    sensitive: bool
    confirm_on_reuse: bool

    model_config = ConfigDict(frozen=True, populate_by_name=True)


class CompiledPolicy(BaseModel):
    """Frozen output of the build step. Sessions pin one by hash."""

    model_config = ConfigDict(frozen=True)

    agent: str
    tenant: str = "default"
    charter: str
    languages: list[str]
    mode: Mode
    #: model names pinned at build, by tier (small: classify/validate/intent, large: generate/plan)
    models: dict[str, str] = Field(default_factory=dict)
    steps: list[CompiledStep]
    allowed_tools: list[str]
    #: datasets named in node configs; the only ones the Tool Gateway resolves for this policy
    datasets: list[str] = Field(default_factory=list)
    memory: list[CompiledMemoryItem]
    defaults: Defaults
    manifest_versions: dict[str, str]
    hash: str
    #: how the conversation is passed to a person; None: never
    handoff: HandoffSpec | None = None
    #: rules that apply on every turn
    rules: list[Rule] = Field(default_factory=list)
    compiled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def has_flow(self) -> bool:
        return bool(self.steps)

    @property
    def fallback_language(self) -> str:
        return self.languages[0]

    def step(self, name: str) -> CompiledStep:
        for s in self.steps:
            if s.name == name:
                return s
        raise KeyError(name)

    def step_fields(self, name: str) -> list[str]:
        """Every field a step takes: its configured `fields` (a form), else the node's declared ones."""
        if not any(s.name == name for s in self.steps):
            return []
        configured = [str(f["name"]) for f in self.step(name).settings.get("fields", []) or []]
        return configured or list(REGISTRY.node(name).cls.fields)

    @property
    def fields(self) -> list[str]:
        """Every field of the flow, in step order."""
        return list(dict.fromkeys(f for s in self.steps for f in self.step_fields(s.name)))

    def producer_of(self, output: str) -> CompiledStep | None:
        for s in self.steps:
            if output in s.produces:
                return s
        return None

    def memory_item(self, name: str) -> CompiledMemoryItem | None:
        return next((m for m in self.memory if m.name == name), None)

    @property
    def sensitive_items(self) -> set[str]:
        return {m.name for m in self.memory if m.sensitive}


def content_hash(payload: dict[str, Any]) -> str:
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()[:16]


def compile_policy(
    policy: AgentPolicy,
    defaults: Defaults = DEFAULTS,
    *,
    tenant: str = "default",
    models: dict[str, str] | None = None,
    datasets: Mapping[str, DatasetKind] | None = None,
) -> CompiledPolicy:
    """`models` pins the tiers for this build ({"small": ..., "large": ...}); default from the environment.
    `datasets` is the tenant's catalog (name → kind); None skips the dataset check (offline build)."""
    errors = validate_policy(policy, defaults, tenant=tenant, datasets=datasets)
    if errors:
        raise PolicyValidationError(errors)
    if models is None:
        from yachat.runtime.llm import ModelConfig

        cfg = ModelConfig.from_env()
        models = {"small": cfg.classify, "large": cfg.generate}
    reqs = resolved_requires(policy)
    mode = policy.effective_mode
    steps: list[CompiledStep] = []
    tool_set: list[str] = []
    used_datasets: set[str] = set()
    for s in policy.flow or []:
        spec = REGISTRY.node(s.name)
        settings = spec.config_model.model_validate(s.settings).model_dump(mode="json")
        used_datasets |= {ds for _, _, ds in dataset_refs(spec.config_model, settings)}
        for f in settings.get("fields", []) or []:
            if f.get("max_reasks") is None:
                f["max_reasks"] = policy.limits.reasks if policy.limits.reasks is not None else defaults.reasks
        steps.append(
            CompiledStep(
                name=s.name,
                node=s.name,
                requires=reqs[s.name],
                mode=s.mode or mode,
                settings=settings,
                tools=list(spec.tools),
                produces=list(spec.produces),
                repairs=resolve_repairs(spec.repairs, defaults, policy.limits.repairs, s.repairs),
                verify=spec.verify if s.verify is None else s.verify,
                rules=s.rules,
                model=s.model,
                high_stakes=s.high_stakes,
            )
        )
        for t in spec.tools:
            if t not in tool_set:
                tool_set.append(t)
    for t in [*policy.tools, *([policy.handoff.notify] if policy.handoff and policy.handoff.notify else [])]:
        if t not in tool_set:
            tool_set.append(t)

    def merge(base: list[str], named: list[str] | None, default: list[str]) -> list[str]:
        out = list(base)
        for n in named if named is not None else default:
            if n not in out:
                out.append(n)
        return out

    resolved = defaults.model_copy(
        update={
            "reasks": policy.limits.reasks if policy.limits.reasks is not None else defaults.reasks,
            "digressions": policy.limits.digressions if policy.limits.digressions is not None else defaults.digressions,
            "max_reopens": policy.limits.reopens if policy.limits.reopens is not None else defaults.max_reopens,
            "planner_step_budget": policy.limits.planner_calls if policy.limits.planner_calls is not None else defaults.planner_step_budget,
            "on_step_enquiries": policy.limits.on_step_enquiries if policy.limits.on_step_enquiries is not None else defaults.on_step_enquiries,
            "repair_limit": policy.limits.repairs,
            "input_guardrails": merge(defaults.base_input_guardrails, policy.guardrails.input, defaults.input_guardrails),
            "output_guardrails": merge(defaults.base_output_guardrails, policy.guardrails.output, defaults.output_guardrails),
        }
    )
    memory = [
        CompiledMemoryItem(
            name=m.field,
            extract=m.extract,
            from_=m.from_,
            schema=type_to_schema(m.type),
            half_life_s=(m.half_life or defaults.memory_half_life).total_seconds(),
            sensitive=(m.sensitive if m.sensitive is not None else m.field in defaults.sensitive_memory),
            confirm_on_reuse=m.confirm_on_reuse,
        )
        for m in policy.memory
    ]
    used = {
        n: v
        for n, v in REGISTRY.manifest(tenant).versions().items()
        if n.split(":", 1)[1] in {*tool_set, *(s.node for s in steps), *resolved.input_guardrails, *resolved.output_guardrails}
    }
    payload = {
        "agent": policy.agent,
        "tenant": tenant,
        "models": models,
        "charter": policy.charter,
        "languages": policy.languages,
        "mode": mode,
        "steps": [_step_payload(s) for s in steps],
        "allowed_tools": tool_set,
        **({"datasets": sorted(used_datasets)} if used_datasets else {}),  # absent when unused: existing hashes stay stable
        "memory": [m.model_dump(mode="json") for m in memory],
        "defaults": resolved.model_dump(mode="json"),
        "manifest": used,
        **({"handoff": _handoff_payload(policy.handoff)} if policy.handoff else {}),  # absent when unused: hashes stay stable
        **({"rules": [r.model_dump(mode="json") for r in policy.rules]} if policy.rules else {}),
    }
    return CompiledPolicy(
        agent=policy.agent,
        tenant=tenant,
        models=models,
        charter=policy.charter,
        languages=policy.languages,
        mode=mode,
        steps=steps,
        allowed_tools=tool_set,
        datasets=sorted(used_datasets),
        memory=memory,
        defaults=resolved,
        manifest_versions=used,
        hash=content_hash(payload),
        handoff=policy.handoff,
        rules=policy.rules,
    )


#: step fields added after policies were first pinned: in the hash only when set, so existing hashes stay stable
_LATER_STEP_FIELDS: dict[str, Any] = {"rules": [], "model": None, "high_stakes": False}


def _handoff_payload(h: HandoffSpec) -> dict[str, Any]:
    out = h.model_dump(mode="json")
    if out.get("timeout") is None:  # added later: in the hash only when set
        out.pop("timeout", None)
    return out


def _step_payload(s: CompiledStep) -> dict[str, Any]:
    out = s.model_dump(mode="json")
    for k, default in _LATER_STEP_FIELDS.items():
        if out[k] == default:
            del out[k]
    return out


def build(text: str, *, tenant: str = "default", datasets: Mapping[str, DatasetKind] | None = None) -> CompiledPolicy:
    return compile_policy(load_policy(text), tenant=tenant, datasets=datasets)


def build_file(path: str, *, tenant: str = "default", datasets: Mapping[str, DatasetKind] | None = None) -> CompiledPolicy:
    with open(path, encoding="utf-8") as f:
        return build(f.read(), tenant=tenant, datasets=datasets)

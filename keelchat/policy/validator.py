"""Semantic validation against the registry. Every error carries the YAML path."""

from __future__ import annotations

from collections.abc import Mapping
from difflib import get_close_matches
from typing import Any

from pydantic import ValidationError

from keelchat.datasets.models import DatasetKind, dataset_refs
from keelchat.policy.defaults import DEFAULTS, Defaults
from keelchat.policy.errors import PolicyError, loc_to_path
from keelchat.policy.models import HANDOFF_ARGS, AgentPolicy
from keelchat.registry.core import REGISTRY


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    white, grey, black = 0, 1, 2
    color = dict.fromkeys(graph, white)
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        color[n] = grey
        stack.append(n)
        for m in graph[n]:
            if m not in color:
                continue
            if color[m] == grey:
                return stack[stack.index(m) :] + [m]
            if color[m] == white:
                c = visit(m)
                if c:
                    return c
        stack.pop()
        color[n] = black
        return None

    for n in graph:
        if color[n] == white:
            c = visit(n)
            if c:
                return c
    return None


def resolved_requires(policy: AgentPolicy) -> dict[str, list[str]]:
    """Without `requires`, a step requires the previous step."""
    out: dict[str, list[str]] = {}
    prev: str | None = None
    for s in policy.flow or []:
        out[s.name] = list(s.requires) if s.requires is not None else ([prev] if prev else [])
        prev = s.name
    return out


def _unknown(kind: str, name: str, known: list[str]) -> str:
    close = get_close_matches(name, known, n=1)
    hint = f"; did you mean {close[0]!r}?" if close else ""
    return f"unregistered {kind} {name!r}{hint} (known: {known})"


def node_repairs(declared: Mapping[str, int], defaults: Defaults) -> dict[str, int]:
    """A node's repair stages and maxima: what its decorator declares, else one "default" stage at the platform value."""
    return dict(declared) if declared else {"default": defaults.repairs}


def resolve_repairs(declared: Mapping[str, int], defaults: Defaults, policy_wide: int | None, step: int | dict[str, int] | None) -> dict[str, int]:
    """Most specific wins: step, then policy limits, then the decorator (or platform) value; never above the maximum."""
    maxima = node_repairs(declared, defaults)
    out = {s: min(policy_wide, m) if policy_wide is not None else m for s, m in maxima.items()}
    if isinstance(step, int):
        out = {s: min(step, m) for s, m in maxima.items()}
    elif step:
        out.update({s: min(n, maxima[s]) for s, n in step.items() if s in maxima})
    return out


def validate_policy(
    policy: AgentPolicy, defaults: Defaults = DEFAULTS, *, tenant: str = "default", datasets: Mapping[str, DatasetKind] | None = None
) -> list[PolicyError]:
    """`datasets` is the tenant's catalog (name → kind). None means an offline build: references are not checked."""
    REGISTRY.discover()
    errors: list[PolicyError] = []
    steps = policy.flow or []
    if policy.mode in ("flow", "adaptive") and not steps:
        errors.append(PolicyError("mode", f"mode {policy.mode!r} needs a `flow`; an agent without steps runs in planner mode"))
    if policy.mode == "planner" and steps:
        errors.append(PolicyError("mode", "mode 'planner' cannot be combined with a `flow`; use flow or adaptive"))
    names = [s.name for s in steps]
    name_set = set(names)
    if len(name_set) != len(names):
        errors.append(PolicyError("flow", f"duplicate steps: {sorted({n for n in names if names.count(n) > 1})}"))

    for i, tool in enumerate(policy.tools):
        if not REGISTRY.has_tool(tool, tenant):
            errors.append(PolicyError(f"tools[{i}]", _unknown("tool", tool, REGISTRY.tool_names(tenant))))

    for i, step in enumerate(steps):
        base = f"flow[{i}]"
        if not REGISTRY.has_node(step.name):
            errors.append(PolicyError(f"{base}", _unknown("step", step.name, REGISTRY.node_names())))
        else:
            spec = REGISTRY.node(step.name)
            try:
                spec.config_model.model_validate(step.settings)
            except ValidationError as e:
                for err in e.errors():
                    sub = loc_to_path(tuple(err["loc"]))
                    errors.append(PolicyError(f"{base}.{step.name}" + (f".{sub}" if sub != "<root>" else ""), err["msg"]))
            else:
                for fname, kind, ds in dataset_refs(spec.config_model, step.settings):
                    path = f"{base}.{step.name}.{fname}"
                    if datasets is None:  # offline build: no catalog to check against
                        continue
                    if ds not in datasets:
                        errors.append(PolicyError(path, _unknown("dataset", ds, sorted(datasets))))
                    elif datasets[ds] != kind:
                        errors.append(PolicyError(path, f"{ds!r} is an {datasets[ds]} dataset; {step.name} needs a {kind} one"))
        if REGISTRY.has_node(step.name) and step.repairs is not None:
            maxima = node_repairs(REGISTRY.node(step.name).repairs, defaults)
            asked = step.repairs if isinstance(step.repairs, dict) else dict.fromkeys(maxima, step.repairs)
            for stage, n in asked.items():
                path = f"{base}.{step.name}.repairs" + (f".{stage}" if isinstance(step.repairs, dict) else "")
                if n < 0:
                    errors.append(PolicyError(path, "repairs cannot be negative"))
                elif stage not in maxima:
                    errors.append(PolicyError(path, f"{step.name} has no repair stage {stage!r}; stages: {sorted(maxima)}"))
                elif n > maxima[stage]:
                    errors.append(PolicyError(path, f"{n} exceeds the node's maximum {maxima[stage]}"))
        for j, dep in enumerate(step.requires or []):
            if dep not in name_set:
                errors.append(PolicyError(f"{base}.{step.name}.requires[{j}]", f"unknown step {dep!r}"))
            elif dep == step.name:
                errors.append(PolicyError(f"{base}.{step.name}.requires[{j}]", "a step cannot require itself"))

    cycle = _find_cycle(resolved_requires(policy))
    if cycle:
        errors.append(PolicyError("flow", f"`requires` has a cycle: {' -> '.join(cycle)}"))

    seen: set[str] = set()
    form_fields: set[str] = set()
    for s in steps:
        if not REGISTRY.has_node(s.name):
            continue
        try:
            cfg = REGISTRY.node(s.name).config_model.model_validate(s.settings).model_dump()
        except ValidationError:
            continue  # reported above with its own path
        form_fields |= {f"{s.name}.{f['name']}" for f in (cfg.get("fields") or [])}
    for i, item in enumerate(policy.memory):
        if item.field in seen:
            errors.append(PolicyError(f"memory[{i}].field", f"duplicate memory field {item.field!r}"))
        seen.add(item.field)
        if item.from_ and item.from_ not in form_fields:
            errors.append(PolicyError(f"memory[{i}].from", f"{item.from_!r} is not a validated form field in this flow; known: {sorted(form_fields)}"))

    for stage in ("input", "output"):
        for i, name in enumerate(getattr(policy.guardrails, stage) or []):
            if not REGISTRY.has_guardrail(name):
                errors.append(PolicyError(f"guardrails.{stage}[{i}]", _unknown("guardrail", name, REGISTRY.guardrail_names())))
            elif REGISTRY.guardrail(name).stage != stage:
                errors.append(PolicyError(f"guardrails.{stage}[{i}]", f"{name!r} is an {REGISTRY.guardrail(name).stage} guardrail"))

    if policy.handoff is not None and (name := policy.handoff.notify) is not None:
        if not REGISTRY.has_tool(name, tenant):
            errors.append(PolicyError("handoff.notify", _unknown("tool", name, REGISTRY.tool_names(tenant))))
        else:
            tool_spec = REGISTRY.tool(name, tenant)
            missing = [a for a in HANDOFF_ARGS if a not in tool_spec.input_model.model_fields]
            if tool_spec.side_effects != "write":
                errors.append(PolicyError("handoff.notify", f"{name!r} must be a write tool (side_effects='write') so the planner never calls it"))
            if missing:
                errors.append(PolicyError("handoff.notify", f"{name!r} does not take the handoff arguments {missing}"))

    for i, lang in enumerate(policy.languages):
        if not (2 <= len(lang) <= 3 and lang.isalpha() and lang.islower()):
            errors.append(PolicyError(f"languages[{i}]", f"expected an ISO 639 code like 'en', got {lang!r}"))
    errors += _rule_errors(policy)
    return errors


def _rule_errors(policy: AgentPolicy) -> list[PolicyError]:
    """Rule names are unique across the agent and its steps; `overrides` names known rules and forms no cycle; a rule that
    hands off has a `handoff` to hand off to."""
    located = [(f"rules[{i}]", r) for i, r in enumerate(policy.rules)] + [
        (f"flow[{i}].{s.name}.rules[{j}]", r) for i, s in enumerate(policy.flow or []) for j, r in enumerate(s.rules)
    ]
    errors: list[PolicyError] = []
    seen: set[str] = set()
    for path, r in located:
        if r.handoff and policy.handoff is None:
            errors.append(PolicyError(f"{path}.handoff", "a rule that hands off needs a top-level `handoff:` (e.g. `handoff: {when: [user_request]}`)"))
        if r.name in seen:
            errors.append(PolicyError(f"{path}.name", f"duplicate rule {r.name!r}"))
        seen.add(r.name)
    for path, r in located:
        for k, other in enumerate(r.overrides):
            if other == r.name:
                errors.append(PolicyError(f"{path}.overrides[{k}]", "a rule cannot override itself"))
            elif other not in seen:
                errors.append(PolicyError(f"{path}.overrides[{k}]", _unknown("rule", other, sorted(seen))))
    graph = {r.name: [o for o in r.overrides if o in seen and o != r.name] for _, r in located}
    if cycle := _find_cycle(graph):
        errors.append(PolicyError("rules", f"overrides form a cycle: {' -> '.join(cycle)}"))
    return errors


__all__ = ["validate_policy", "resolved_requires", "node_repairs", "resolve_repairs", "Any"]

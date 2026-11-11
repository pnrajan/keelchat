"""JSON Schema for the policy editor, generated from the policy models and the registry. It accepts exactly what the
validator's shape checks accept (tests/test_policy.py checks every example), so the editor never flags a valid policy;
cross-field rules (unknown datasets, repair maxima, `requires` targets) stay with the build."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, TypeAdapter

from keelchat.datasets.models import field_dataset_kind
from keelchat.policy.defaults import DEFAULTS
from keelchat.policy.durations import Duration
from keelchat.policy.models import HandoffSpec, Limits, MemoryField, Rule
from keelchat.registry.core import REGISTRY

_MODES = {"enum": ["flow", "planner", "adaptive"]}
_REPAIRS = {
    "description": "repair attempts: one number for every stage, or per stage ({sql: 1}); never above the node's maximum",
    "oneOf": [{"type": "integer", "minimum": 0}, {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}}],
}


def _model_schema(model: type[BaseModel], defs: dict[str, Any]) -> dict[str, Any]:
    """The model's schema with its $defs hoisted to the root, where `#/$defs/...` refs resolve."""
    schema = model.model_json_schema(by_alias=True)
    defs.update(schema.pop("$defs", {}))
    return schema


def _rules(defs: dict[str, Any]) -> dict[str, Any]:
    rule = _model_schema(Rule, defs)
    rule["description"] = "when the condition holds for the user's message, the reply follows `then`"
    return {"type": "array", "items": rule}


def _step_schema(name: str, defs: dict[str, Any]) -> dict[str, Any]:
    spec = REGISTRY.node(name)
    cfg = _model_schema(spec.config_model, defs)
    props = cfg.setdefault("properties", {})
    for fname, info in spec.config_model.model_fields.items():
        if (kind := field_dataset_kind(info)) and fname in props:
            props[fname]["x-dataset"] = kind  # the editor offers datasets of this kind
    props |= {
        "requires": {"type": "array", "items": {"type": "string"}},
        "mode": _MODES,
        "repairs": _REPAIRS,
        "verify": {"type": "boolean", "description": "check this step's replies before they are sent (default true); false passes them through"},
        "rules": {**_rules(defs), "description": "rules that apply only while this step is asking"},
        "model": {"enum": ["small", "large"], "description": "run this step on the small (cheaper) or large (stronger) model"},
        "high_stakes": {"type": "boolean", "description": "write this step's replies with the large model and always check them"},
        "think": {"type": "boolean", "description": "plan each reply before it is written: what the user needs, what is unknown, the rules it must follow"},
    }
    return {
        "type": "object",
        "properties": {name: {"anyOf": [cfg, {"type": "null"}]} if not cfg.get("required") else cfg},
        "required": [name],
        "additionalProperties": False,
        "title": name,
        "description": spec.description,
    }


def policy_json_schema() -> dict[str, Any]:
    REGISTRY.discover()
    defs: dict[str, Any] = {}
    duration = TypeAdapter(Duration).json_schema()
    steps = [_step_schema(n, defs) for n in REGISTRY.node_names()]
    memory = _model_schema(MemoryField, defs)
    memory["properties"]["half_life"] = {"anyOf": [duration, {"type": "null"}]}
    memory["properties"]["type"] = {"description": "string, integer, number, boolean, email, a|b|c choices, list[...]"}
    limits = _model_schema(Limits, defs)
    handoff = _model_schema(HandoffSpec, defs)
    writes = [n for n in REGISTRY.tool_names() if REGISTRY.tool(n).side_effects == "write"]
    handoff["properties"]["notify"] = {
        "anyOf": [{"type": "string", "enum": writes}, {"type": "null"}],
        "description": "also send each handoff to a helpdesk through this write tool (e.g. handoff_webhook)",
    }
    handoff["description"] = "pass the conversation to a person: it lands in the handoff inbox; when (`when`) and who else is told (`notify`)"
    out: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "AgentPolicy",
        "type": "object",
        "additionalProperties": False,
        "required": ["agent", "charter"],
        "properties": {
            "agent": {"type": "string", "pattern": "^[a-z0-9][a-z0-9-]*$", "description": "the agent's id: lowercase letters, digits, dashes"},
            "charter": {"type": "string", "minLength": 1, "description": "what the agent does, its tone and what is out of scope"},
            "languages": {"type": "array", "items": {"type": "string"}, "minItems": 1, "default": ["en"]},
            "tools": {"type": "array", "items": {"enum": REGISTRY.tool_names()}, "description": "extra tools; flow steps bring their own"},
            "flow": {"type": "array", "description": "the steps, in order", "items": {"oneOf": [{"enum": REGISTRY.node_names()}, *steps]}},
            "guardrails": {
                "type": "object",
                "additionalProperties": False,
                "description": "named checks; the base set (prompt_injection, pii) always runs",
                "properties": {
                    "input": {"type": "array", "items": {"enum": [n for n in REGISTRY.guardrail_names() if REGISTRY.guardrail(n).stage == "input"]}},
                    "output": {"type": "array", "items": {"enum": [n for n in REGISTRY.guardrail_names() if REGISTRY.guardrail(n).stage == "output"]}},
                },
            },
            "limits": limits,
            "memory": {"type": "array", "items": {"oneOf": [{"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}, memory]}},
            "mode": _MODES,
            "handoff": handoff,
            "rules": {**_rules(defs), "description": "plain-language rules, used on any turn where their condition holds"},
            "small_talk": {
                "enum": ["off", "brief", "warm"],
                "default": "off",
                "description": "friendly small talk: off (counted as wandering off), brief (one friendly line, then back to the task), "
                "warm (also picks up what they mentioned)",
            },
        },
        "x-registry": REGISTRY.manifest().model_dump(mode="json"),
        "x-defaults": DEFAULTS.model_dump(mode="json"),
    }
    if defs:
        out["$defs"] = defs
    return out

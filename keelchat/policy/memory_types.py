"""Memory field `type` specs → JSON Schema. `string | number | boolean | list[string] | "a|b|c" | {key: spec, ...}`."""

from __future__ import annotations

import re
from typing import Any

_PRIMITIVES = {"string": {"type": "string"}, "number": {"type": "number"}, "integer": {"type": "integer"}, "boolean": {"type": "boolean"}}
_LIST = re.compile(r"^list\[(.+)\]$")


def type_to_schema(spec: Any) -> dict[str, Any]:
    if spec is None:
        return {"type": "string"}
    if isinstance(spec, str):
        s = spec.strip()
        if s in _PRIMITIVES:
            return dict(_PRIMITIVES[s])
        m = _LIST.match(s)
        if m:
            return {"type": "array", "items": type_to_schema(m.group(1))}
        if "|" in s:
            return {"type": "string", "enum": [x.strip() for x in s.split("|") if x.strip()]}
        raise ValueError(f"unknown type {spec!r}; use string, number, boolean, list[...], 'a|b|c' or a mapping")
    if isinstance(spec, dict):
        props = {str(k): type_to_schema(v) for k, v in spec.items()}
        return {"type": "object", "properties": props, "required": sorted(props), "additionalProperties": False}
    raise ValueError(f"unknown type {spec!r}")


def validate_value(schema: dict[str, Any], value: Any) -> bool:
    """Minimal structural check for the schemas we generate. Values that fail are dropped by the memory writer."""
    t = schema.get("type")
    if t == "string":
        return isinstance(value, str) and ("enum" not in schema or value in schema["enum"])
    if t == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "array":
        return isinstance(value, list) and all(validate_value(schema["items"], v) for v in value)
    if t == "object":
        if not isinstance(value, dict) or set(value) - set(schema["properties"]):
            return False
        return all(k in value and validate_value(s, value[k]) for k, s in schema["properties"].items())
    return False

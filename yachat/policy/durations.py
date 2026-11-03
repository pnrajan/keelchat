"""Duration strings like ``30d``, ``12h`` parsed to ``timedelta``."""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated

from pydantic import BeforeValidator, PlainSerializer, WithJsonSchema

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_PATTERN = re.compile(r"^(\d+(?:\.\d+)?)([smhdw])$")


def parse_duration(value: object) -> timedelta:
    if isinstance(value, timedelta):
        return value
    if isinstance(value, int | float):
        return timedelta(seconds=float(value))
    if not isinstance(value, str):
        raise ValueError("duration must be a string like '30d' or '12h'")
    m = _PATTERN.match(value.strip())
    if not m:
        raise ValueError(f"invalid duration {value!r}; expected e.g. '30d', '12h', '15m'")
    return timedelta(seconds=float(m.group(1)) * _UNITS[m.group(2)])


def format_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    for unit, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60)):
        if secs and secs % size == 0:
            return f"{secs // size}{unit}"
    return f"{secs}s"


Duration = Annotated[
    timedelta,
    BeforeValidator(parse_duration),
    PlainSerializer(format_duration, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "pattern": r"^\d+(\.\d+)?[smhdw]$", "examples": ["30d", "12h"]}),
]

"""Structured JSON logs: session_id, turn_id, policy_hash, stage, latency_ms on every line. No secrets, no personal data."""

from __future__ import annotations

import contextvars
import json
import logging
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

_ctx: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("log_ctx", default=None)
_FIELDS = ("session_id", "turn_id", "policy_hash", "stage")
REDACTED = "[redacted]"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cur = _ctx.get() or {}
        base: dict[str, Any] = {k: cur.get(k) for k in _FIELDS}
        t0 = cur.get("_t0")
        base["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2) if t0 else None
        base.update({"level": record.levelname, "logger": record.name, "msg": record.getMessage()})
        extra = getattr(record, "data", None)
        if extra:
            base.update(extra)
        if record.exc_info:
            base["exc"] = self.formatException(record.exc_info)
        return json.dumps(base, default=str)


def configure(level: int = logging.INFO) -> None:
    root = logging.getLogger("yachat")
    if not root.handlers:
        h = logging.StreamHandler()
        h.setFormatter(JsonFormatter())
        root.addHandler(h)
    root.setLevel(level)


class Logger:
    def __init__(self, name: str):
        self._log = logging.getLogger(name)

    def _emit(self, level: int, msg: str, data: dict[str, Any]) -> None:
        self._log.log(level, msg, extra={"data": data})

    def info(self, msg: str, **data: Any) -> None:
        self._emit(logging.INFO, msg, data)

    def warning(self, msg: str, **data: Any) -> None:
        self._emit(logging.WARNING, msg, data)

    def error(self, msg: str, **data: Any) -> None:
        self._emit(logging.ERROR, msg, data)

    def debug(self, msg: str, **data: Any) -> None:
        self._emit(logging.DEBUG, msg, data)


def get_logger(name: str) -> Logger:
    return Logger(f"yachat.{name}")


@contextmanager
def bound(**kv: Any) -> Iterator[None]:
    cur = dict(_ctx.get() or {})
    cur.update(kv)
    if "_t0" not in cur:
        cur["_t0"] = time.perf_counter()
    token = _ctx.set(cur)
    try:
        yield
    finally:
        _ctx.reset(token)


def redact_fields(values: dict[str, Any], sensitive: set[str]) -> dict[str, Any]:
    """Field names are loggable; values never are for sensitive items. Non-sensitive values are summarised as lengths."""
    return {k: (REDACTED if k in sensitive else f"<{len(str(v))} chars>") for k, v in values.items()}


class Metrics:
    """One latency metric per stage (ms) plus counters. In-process; export elsewhere."""

    def __init__(self) -> None:
        self._samples: dict[str, list[float]] = defaultdict(list)
        self.counters: dict[str, int] = defaultdict(int)

    def record(self, stage: str, ms: float) -> None:
        self._samples[stage].append(ms)

    def incr(self, name: str, by: int = 1) -> None:
        self.counters[name] += by

    def percentile(self, stage: str, p: float) -> float | None:
        s = sorted(self._samples.get(stage, []))
        if not s:
            return None
        k = max(0, min(len(s) - 1, round(p / 100 * (len(s) - 1))))
        return s[k]

    def p95(self, stage: str) -> float | None:
        return self.percentile(stage, 95)

    def snapshot(self) -> dict[str, Any]:
        return {
            "stages": {k: {"n": len(v), "p50": self.percentile(k, 50), "p95": self.percentile(k, 95)} for k, v in self._samples.items()},
            "counters": dict(self.counters),
        }


class Timer:
    ms: float = 0.0

    def __enter__(self) -> Timer:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        self.ms = (time.perf_counter() - self._t0) * 1000

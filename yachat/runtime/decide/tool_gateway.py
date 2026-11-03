"""Tool Gateway: the only path from nodes and the planner to tools.

Allow list, dataset allow list, candidate-only action guard, per-tool timeout, one retry with backoff, circuit breaker, structured errors,
sensitive values never sent, output wrapped as untrusted data. ActionRunner runs candidate-only actions.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from pydantic import BaseModel, ValidationError

from yachat.capabilities.tools import ToolError
from yachat.datasets.models import Dataset
from yachat.logging import get_logger
from yachat.registry.core import REGISTRY, Registry, RegistryError, ToolSpec
from yachat.runtime.llm import untrusted

log = get_logger("gateway")

ToolImpl = Callable[..., Awaitable[Any]]


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: Any = None
    error: str | None = None
    error_kind: str | None = None
    attempts: int = 0
    ms: float = 0.0
    translate: bool = True

    def as_prompt_data(self) -> str:
        body = json.dumps(self.data, default=_dump) if self.ok else json.dumps({"error": self.error_kind or "failed"})
        return untrusted("tool_output", body, tool=self.tool)


def _dump(o: Any) -> Any:
    return o.model_dump(mode="json") if isinstance(o, BaseModel) else str(o)


def to_plain(data: Any) -> Any:
    if isinstance(data, BaseModel):
        return data.model_dump(mode="json")
    if isinstance(data, list):
        return [to_plain(x) for x in data]
    if isinstance(data, dict):
        return {k: to_plain(v) for k, v in data.items()}
    return data


def scrub(args: dict[str, Any], sensitive: set[str]) -> dict[str, Any]:
    """Sensitive memory values never leave the runtime."""

    def clean(v: Any) -> Any:
        if isinstance(v, str):
            for s in sensitive:  # also inside a longer string (an answer the model normalised, a planner query)
                if s and s in v:
                    v = "[withheld]" if v == s else v.replace(s, "[withheld]")
            return v
        if isinstance(v, dict):
            return {k: clean(x) for k, x in v.items()}
        if isinstance(v, list):
            return [clean(x) for x in v]
        return v

    return {k: clean(v) for k, v in args.items()}


@dataclass
class _Breaker:
    failures: int = 0
    opened_at: float | None = None


@dataclass
class ToolGateway:
    allowed: set[str]
    impls: dict[str, ToolImpl] = dc_field(default_factory=dict)
    tenant: str = "default"
    #: where tools are looked up; the contract suite passes a scratch registry holding one candidate spec
    registry: Registry = dc_field(default_factory=lambda: REGISTRY)
    default_timeout_s: float | None = None
    retry_backoff_s: float = 0.05
    breaker_threshold: int = 3
    breaker_cooldown_s: float = 30.0
    sensitive_values: set[str] = dc_field(default_factory=set)
    #: the policy's datasets, resolved for this tenant: a dataset argument may only name one of these
    datasets: dict[str, Dataset] = dc_field(default_factory=dict)
    _breakers: dict[str, _Breaker] = dc_field(default_factory=dict)
    calls: list[tuple[str, dict[str, Any]]] = dc_field(default_factory=list)
    rejected: list[tuple[str, str]] = dc_field(default_factory=list)
    #: every call's outcome this turn, for the audit log
    results: list[ToolResult] = dc_field(default_factory=list)

    def is_allowed(self, name: str) -> bool:
        return name in self.allowed

    def spec(self, name: str) -> ToolSpec:
        return self.registry.tool(name, self.tenant)

    def _breaker(self, name: str) -> _Breaker:
        return self._breakers.setdefault(name, _Breaker())

    def _open(self, b: _Breaker) -> bool:
        if b.opened_at is None:
            return False
        if time.monotonic() - b.opened_at >= self.breaker_cooldown_s:
            b.opened_at, b.failures = None, 0
            return False
        return True

    async def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        r = await self._call(name, args)
        self.results.append(r)
        return r

    async def _call(self, name: str, args: dict[str, Any]) -> ToolResult:
        t0 = time.perf_counter()
        if self.registry.has_action(name):
            log.error("candidate-only action requested through the gateway; rejected", action=name)
            self.rejected.append((name, "candidate_only"))
            return ToolResult(name, False, error=f"{name!r} is a candidate-only action, not a tool", error_kind="candidate_only")
        try:
            spec = self.spec(name)
        except RegistryError:
            return ToolResult(name, False, error=f"unknown tool {name!r}", error_kind="unknown")
        if name not in self.allowed:
            log.warning("tool not allowed", tool=name)
            self.rejected.append((name, "not_allowed"))
            return ToolResult(name, False, error=f"tool {name!r} is not in this agent's allow list", error_kind="not_allowed", translate=spec.translate)
        b = self._breaker(name)
        if self._open(b):
            log.warning("circuit open", tool=name)
            return ToolResult(name, False, error="circuit open", error_kind="circuit_open", translate=spec.translate)
        try:
            # only the arguments actually given: omitted ones take the tool's own default, never an explicit null
            validated = spec.input_model.model_validate(scrub(args, self.sensitive_values)).model_dump(exclude_unset=True)
        except ValidationError as e:
            return ToolResult(name, False, error=f"invalid arguments: {e.error_count()} error(s)", error_kind="invalid_args", translate=spec.translate)
        if spec.dataset is not None and validated.get("dataset") is not None:
            ds = self.datasets.get(validated["dataset"])
            if ds is None or ds.kind != spec.dataset:
                log.warning("dataset not allowed", tool=name, dataset=validated["dataset"])
                self.rejected.append((name, "dataset_not_allowed"))
                return ToolResult(name, False, error=f"dataset {validated['dataset']!r} is not available to this agent", error_kind="dataset_not_allowed")
            validated["dataset"] = ds
        if spec.repairs:  # the decorator's values are the default and the maximum: a caller (or the planner) can only lower them
            asked = validated.get("repairs") or {}
            if set(asked) - set(spec.repairs):
                return ToolResult(name, False, error=f"unknown repair stages {sorted(set(asked) - set(spec.repairs))}", error_kind="invalid_args")
            validated["repairs"] = {s: max(0, min(int(asked.get(s, m)), m)) for s, m in spec.repairs.items()}
        impl = self.impls.get(name, spec.fn)
        timeout = self.default_timeout_s or spec.timeout_s
        self.calls.append((name, validated))
        last = ("unknown", "unknown")
        attempts = 0
        for attempt in range(spec.retries + 1):
            attempts = attempt + 1
            try:
                data = await asyncio.wait_for(impl(**validated), timeout=timeout)
                b.failures = 0
                ms = (time.perf_counter() - t0) * 1000
                log.info("tool ok", tool=name, attempts=attempts, tool_ms=round(ms, 2))
                return ToolResult(name, True, data=to_plain(data), attempts=attempts, ms=ms, translate=spec.translate)
            except TimeoutError:
                last = ("timeout", f"timed out after {timeout}s")
            except ToolError as e:
                last = (e.kind, str(e))
            except Exception as e:  # isolation: a buggy tool never crashes the turn
                last = ("exception", f"{e.__class__.__name__}: {e}")
            log.error("tool failed", tool=name, attempt=attempts, kind=last[0], error=last[1])
            if attempt < spec.retries:
                await asyncio.sleep(self.retry_backoff_s)
        b.failures += 1
        if b.failures >= self.breaker_threshold:
            b.opened_at = time.monotonic()
            log.error("circuit opened", tool=name, failures=b.failures)
        return ToolResult(name, False, error=last[1], error_kind=last[0], attempts=attempts, ms=(time.perf_counter() - t0) * 1000, translate=spec.translate)


@dataclass
class ActionRunner:
    """Runs candidate-only actions. The caller must prove the candidate took the action (`candidate_action == name`)."""

    impls: dict[str, ToolImpl] = dc_field(default_factory=dict)
    sensitive_values: set[str] = dc_field(default_factory=set)
    calls: list[tuple[str, dict[str, Any]]] = dc_field(default_factory=list)

    async def run(self, name: str, args: dict[str, Any], candidate_action: str | None) -> ToolResult:
        t0 = time.perf_counter()
        if not REGISTRY.has_action(name):
            return ToolResult(name, False, error=f"unknown action {name!r}", error_kind="unknown")
        if candidate_action != name:
            log.error("action without the candidate's own action; rejected", action=name)
            return ToolResult(name, False, error="only the candidate can trigger this action", error_kind="candidate_only")
        spec = REGISTRY.action(name)
        try:
            validated = spec.input_model.model_validate(scrub(args, self.sensitive_values)).model_dump()
        except ValidationError as e:
            return ToolResult(name, False, error=f"invalid arguments: {e.error_count()} error(s)", error_kind="invalid_args")
        impl = self.impls.get(name, spec.fn)
        self.calls.append((name, validated))
        try:
            data = await asyncio.wait_for(impl(**validated), timeout=spec.timeout_s)
            log.info("action ok", action=name, action_ms=round((time.perf_counter() - t0) * 1000, 2))
            return ToolResult(name, True, data=to_plain(data), attempts=1, ms=(time.perf_counter() - t0) * 1000)
        except TimeoutError:
            kind, err = "timeout", f"timed out after {spec.timeout_s}s"
        except ToolError as e:
            kind, err = e.kind, str(e)
        except Exception as e:
            kind, err = "exception", f"{e.__class__.__name__}: {e}"
        log.error("action failed", action=name, kind=kind, error=err)
        return ToolResult(name, False, error=err, error_kind=kind, attempts=1, ms=(time.perf_counter() - t0) * 1000)

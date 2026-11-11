"""Contract suite every registered capability must pass: valid schema, fake call succeeds, timeout honored,
no direct network access outside the Tool Gateway."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Any

from keelchat.datasets.models import Dataset, DatasetIn, DatasetKind, DocumentSource, StructuredDataset, TableSource, UnstructuredDataset
from keelchat.logging import Metrics
from keelchat.policy.compiler import CompiledPolicy, build
from keelchat.registry.core import (
    REGISTRY,
    ActionSpec,
    GuardContext,
    GuardrailSpec,
    GuardResult,
    NodeResult,
    NodeSpec,
    PreProcessorSpec,
    Registry,
    ToolSpec,
)
from keelchat.runtime.context import InboundMessage, Services, TurnContext
from keelchat.runtime.decide.tool_gateway import ActionRunner, ToolGateway
from keelchat.runtime.llm import LLMClient, ModelConfig
from keelchat.state.models import OpenQuestion, SessionState


class ContractViolation(AssertionError):
    pass


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ContractViolation(msg)


def _valid_json_schema(schema: dict[str, Any]) -> bool:
    return isinstance(schema, dict) and ("type" in schema or "$ref" in schema or "anyOf" in schema or "oneOf" in schema or schema == {})


@contextmanager
def no_network() -> Any:
    """Any socket creation during the block raises. Tools go through the gateway with fakes; nothing else touches the network."""
    real = socket.socket

    def blocked(*a: Any, **k: Any) -> Any:
        raise ContractViolation("direct network access outside the Tool Gateway")

    socket.socket = blocked  # type: ignore[misc,assignment]
    try:
        yield
    finally:
        socket.socket = real  # type: ignore[misc]


MINIMAL = """
agent: contract-agent
charter: contract test
tools: [knowledge_search]
flow:
  - greet_consent
  - job_search
  - form_fill:
      fields:
        - {name: full_name, ask: "What is your full name?", expect: A name}
"""


def make_context(
    text: str = "hello",
    open_question: OpenQuestion | None = None,
    policy: CompiledPolicy | None = None,
    impls: dict[str, Callable[..., Awaitable[Any]]] | None = None,
    llm: LLMClient | None = None,
) -> TurnContext:
    from keelchat.runtime.llm_factory import llm_from_env
    from keelchat.testing.fakes import FakeTools

    policy = policy or build(MINIMAL)
    state = SessionState(user_id="contract-user", agent=policy.agent, policy_hash=policy.hash, channel="web", open_question=open_question)
    for s in policy.steps:
        state.step(s.name)
    gateway = ToolGateway(set(policy.allowed_tools), impls if impls is not None else FakeTools().impls())
    ctx = TurnContext(
        state=state,
        policy=policy,
        inbound=InboundMessage(text, "web", "contract-user"),
        services=Services(llm or llm_from_env(), ModelConfig.from_env(), gateway, Metrics()),
    )
    ctx.system_prompt = "contract"
    return ctx


def _example_value(annotation: Any) -> Any:
    """A minimal valid value for a required argument, by its annotation."""
    origin = getattr(annotation, "__origin__", annotation)
    for typ, value in ((bool, False), (int, 0), (float, 0.0), (list, []), (dict, {})):
        if origin is typ:
            return value
    return "x"


def _dataset_kind(annotation: Any, metadata: list[Any]) -> DatasetKind | None:
    from keelchat.datasets.models import DatasetRef, _ref_of

    ref = next((m for m in metadata if isinstance(m, DatasetRef)), None) or _ref_of(annotation)
    return ref.kind if ref is not None else None


def sample_dataset(kind: DatasetKind, name: str = "x") -> Dataset:
    """A dataset the contract suite resolves dataset arguments to; never read (the call is faked or fails offline)."""
    spec = (
        StructuredDataset(tables=[TableSource(name="t", uri="s3://contract/t.csv")])
        if kind == "structured"
        else UnstructuredDataset(documents=[DocumentSource(uri="s3://contract/d.txt")])
    )
    return Dataset.create("default", name, DatasetIn(spec=spec))


async def check_tool(spec: ToolSpec, fake_impl: Callable[..., Awaitable[Any]] | None = None) -> None:
    _require(_valid_json_schema(spec.input_schema), f"{spec.name}: input schema is not valid JSON Schema")
    _require(_valid_json_schema(spec.output_schema), f"{spec.name}: output schema is not valid JSON Schema")
    _require(spec.timeout_s > 0, f"{spec.name}: timeout_s must be > 0")
    _require(spec.side_effects in ("none", "write"), f"{spec.name}: bad properties")
    example = {k: _example_value(v.annotation) for k, v in spec.input_model.model_fields.items() if v.is_required()}
    datasets = {}
    if spec.dataset is not None:
        example["dataset"] = "x"
        datasets = {"x": sample_dataset(spec.dataset)}
    impls = {spec.name: fake_impl} if fake_impl else {}
    scratch = Registry()  # the spec may not be registered yet (a tenant registration is checked before it goes live)
    scratch.add_tool(spec)
    tenant = spec.tenant or "default"
    gw = ToolGateway({spec.name}, impls, retry_backoff_s=0.0, default_timeout_s=0.5, registry=scratch, tenant=tenant, datasets=datasets)
    with no_network():
        res = await gw.call(spec.name, example)
    if fake_impl is not None:
        _require(res.ok or res.error_kind == "approval_required", f"{spec.name}: fake call failed: {res.error}")
    else:
        _require(
            res.error_kind in ("config", "upstream", "approval_required") or res.ok,
            f"{spec.name}: must fail with a structured ToolError, got {res.error_kind}",
        )

    async def slow(**_: Any) -> Any:
        await asyncio.sleep(0.2)
        return None

    gw2 = ToolGateway({spec.name}, {spec.name: slow}, retry_backoff_s=0.0, default_timeout_s=0.01, registry=scratch, tenant=tenant, datasets=datasets)
    res2 = await gw2.call(spec.name, example)
    _require(res2.error_kind == "timeout", f"{spec.name}: gateway timeout not honored ({res2.error_kind})")


async def check_node(spec: NodeSpec) -> None:
    _require(_valid_json_schema(spec.config_model.model_json_schema()), f"{spec.name}: config schema invalid")
    _require(spec.config_model.model_config.get("extra") == "forbid", f"{spec.name}: config model must reject unknown keys")
    for t in spec.tools:
        _require(REGISTRY.has_tool(t), f"{spec.name}: declares unregistered tool {t!r}")
    ctx = make_context()
    in_policy = any(s.name == spec.name for s in ctx.policy.steps)
    settings: dict[str, Any] = dict(ctx.policy.step(spec.name).settings) if in_policy else {}
    # a required dataset reference gets a sample dataset of its kind, which the gateway can resolve
    for fname, info in spec.config_model.model_fields.items():
        kind = _dataset_kind(info.annotation, info.metadata)
        if kind is not None and fname not in settings:
            settings[fname] = "x"
            ctx.services.gateway.datasets["x"] = sample_dataset(kind)
    ctx.services.gateway.allowed |= set(spec.tools)
    cfg = spec.config_model.model_validate(settings)
    r = await asyncio.wait_for(spec.cls().act(ctx, cfg), timeout=spec.cls.timeout_s)  # may call the configured model
    _require(isinstance(r, NodeResult) and isinstance(r.text, str), f"{spec.name}.act must return NodeResult with str text")


async def check_guardrail(spec: GuardrailSpec) -> None:
    _require(spec.stage in ("input", "output"), f"{spec.name}: stage must be input|output")
    g = spec.cls()
    for text in ("", "Hello, I am looking for a backend role in Austin.", "x" * 10_000):
        with no_network():
            r = await asyncio.wait_for(g.check(text, GuardContext()), timeout=spec.cls.timeout_s)
        _require(isinstance(r, GuardResult), f"{spec.name}.check must return GuardResult")
        if r.flagged:
            _require(bool(r.reason), f"{spec.name}: flagged results need a reason")
            if r.redacted is not None:
                _require(r.redacted != text, f"{spec.name}: redacted text must differ")


async def check_action(spec: ActionSpec, fake_impl: Callable[..., Awaitable[Any]] | None = None) -> None:
    _require(_valid_json_schema(spec.input_schema) and _valid_json_schema(spec.output_schema), f"{spec.name}: schemas must be valid JSON Schema")
    _require(spec.by == "candidate" and spec.timeout_s > 0, f"{spec.name}: bad action properties")
    example = {k: ("x" if v.annotation is str else {}) for k, v in spec.input_model.model_fields.items() if v.is_required()}
    runner = ActionRunner({spec.name: fake_impl} if fake_impl else {})
    rejected = await runner.run(spec.name, example, candidate_action=None)
    _require(rejected.error_kind == "candidate_only", f"{spec.name}: must be rejected without the candidate's action")
    gw = ToolGateway({spec.name})
    _require((await gw.call(spec.name, example)).error_kind == "candidate_only", f"{spec.name}: the gateway must reject actions")
    if fake_impl is not None:
        with no_network():
            ok = await runner.run(spec.name, example, candidate_action=spec.name)
        _require(ok.ok, f"{spec.name}: fake call failed: {ok.error}")


async def check_preprocessor(spec: PreProcessorSpec) -> None:
    _require(isinstance(spec.order, int) and spec.cls.timeout_s > 0, f"{spec.name}: order must be an int and timeout_s > 0")
    ctx = make_context("hello there")
    out = await asyncio.wait_for(spec.cls().run(ctx), timeout=spec.cls.timeout_s)  # may call the configured model
    _require(out is ctx, f"{spec.name}.run must return the TurnContext it received")


async def check_capability(kind: str, name: str, fake_impl: Callable[..., Awaitable[Any]] | None = None) -> None:
    if kind == "tool":
        await check_tool(REGISTRY.tool(name), fake_impl)
    elif kind == "action":
        await check_action(REGISTRY.action(name), fake_impl)
    elif kind == "preprocessor":
        await check_preprocessor(REGISTRY.preprocessor(name))
    elif kind == "node":
        await check_node(REGISTRY.node(name))
    elif kind == "guardrail":
        await check_guardrail(REGISTRY.guardrail(name))
    else:  # pragma: no cover
        raise ContractViolation(f"unknown kind {kind}")


def all_registered() -> list[tuple[str, str]]:
    REGISTRY.discover()
    return [(e.kind, e.name) for e in REGISTRY.manifest().entries]

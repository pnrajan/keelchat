"""One registry for tools, actions, nodes, pre-processors and guardrails. Decorators attach metadata and register."""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, get_type_hints

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, create_model

from keelchat.datasets.models import NAME_PATTERN, Dataset, DatasetKind

if TYPE_CHECKING:  # pragma: no cover
    from keelchat.runtime.context import TurnContext


class CapabilityConfig(BaseModel):
    """Base for a node's inline settings model. Unknown keys rejected."""

    model_config = ConfigDict(extra="forbid")


class EmptyConfig(CapabilityConfig):
    pass


SideEffects = Literal["none", "write"]
GuardStage = Literal["input", "output"]
ToolFn = Callable[..., Awaitable[Any]]


def _schema_hash(schema: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(schema, sort_keys=True, default=str).encode()).hexdigest()[:12]


def _input_model_from_signature(name: str, fn: ToolFn) -> type[BaseModel]:
    hints = get_type_hints(fn)
    fields: dict[str, Any] = {}
    for pname, param in inspect.signature(fn).parameters.items():
        ann: Any = hints.get(pname, Any)
        if ann is Dataset or ann == Dataset | None:  # the model names a dataset; the gateway passes the resolved one
            ann = DatasetName if ann is Dataset else DatasetName | None
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[pname] = (ann, default)
    model: type[BaseModel] = create_model(f"{name}_input", __config__=ConfigDict(extra="forbid"), **fields)
    return model


DatasetName = Annotated[str, Field(pattern=NAME_PATTERN)]

_JSON_TYPES: dict[str, Any] = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}


def model_from_json_schema(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Input model for a tool defined by a JSON Schema (MCP, tenant HTTP tools): top-level properties become fields."""
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for pname, prop in (schema.get("properties") or {}).items():
        ann = _JSON_TYPES.get(str(prop.get("type", "")), Any)
        fields[pname] = (ann, ...) if pname in required else (ann | None, prop.get("default"))
    model: type[BaseModel] = create_model(f"{name}_input", __config__=ConfigDict(extra="forbid"), **fields)
    return model


def _output_schema(fn: ToolFn) -> dict[str, Any]:
    ret = get_type_hints(fn).get("return", Any)
    try:
        return TypeAdapter(ret).json_schema()
    except Exception:
        return {}


# ---------------------------------------------------------------- tools


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: ToolFn
    input_model: type[BaseModel]
    output_schema: dict[str, Any]
    translate: bool = True
    timeout_s: float = 5.0
    #: gateway attempts after the first failure (timeout, ToolError, exception)
    retries: int = 1
    side_effects: SideEffects = "none"
    version: str = "1"
    description: str = ""
    origin: Literal["platform", "mcp"] = "platform"
    #: tenant tools are visible only to that tenant's agents
    tenant: str | None = None
    url: str = ""
    #: the kind of dataset its `dataset` argument names; the gateway resolves it against the policy's datasets
    dataset: DatasetKind | None = None
    #: repair attempts per stage: the default and the maximum; the gateway fills and caps its `repairs` argument
    repairs: Mapping[str, int] = dc_field(default_factory=dict)

    @property
    def source(self) -> Literal["platform", "mcp", "tenant"]:
        return "tenant" if self.tenant is not None else self.origin

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def schema_hash(self) -> str:
        return _schema_hash({"in": self.input_schema, "out": self.output_schema, **({"repairs": dict(self.repairs)} if self.repairs else {})})


# ---------------------------------------------------------------- actions (candidate-only; not tools)


@dataclass(frozen=True)
class ActionSpec:
    """Runs only when the candidate takes the action themselves. Nodes and the planner cannot call it."""

    name: str
    fn: ToolFn
    input_model: type[BaseModel]
    output_schema: dict[str, Any]
    by: Literal["candidate"] = "candidate"
    timeout_s: float = 10.0
    version: str = "1"
    description: str = ""

    @property
    def input_schema(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()

    def schema_hash(self) -> str:
        return _schema_hash({"in": self.input_schema, "out": self.output_schema, "by": self.by})


# ---------------------------------------------------------------- pre-processors


class PreProcessor:
    """An Understand sub-step. Registered with @preprocessor(name, order); run in ascending order."""

    name: ClassVar[str] = ""
    timeout_s: ClassVar[float] = 30.0  # some pre-processors call the model (shorthand, language)

    async def run(self, ctx: TurnContext) -> TurnContext:
        raise NotImplementedError


@dataclass(frozen=True)
class PreProcessorSpec:
    name: str
    cls: type[PreProcessor]
    order: int
    version: str = "1"
    description: str = ""

    def schema_hash(self) -> str:
        return _schema_hash({"order": self.order})


# ---------------------------------------------------------------- nodes


@dataclass
class NodeResult:
    """`text` is the plain reply: the facts and the question, said as is when replies are not rewritten (or the model is
    down). `goal` says what the reply must achieve, in a sentence; the Rewriter writes the words from it."""

    text: str
    goal: str = ""
    items: list[dict[str, Any]] = dc_field(default_factory=list)
    #: the question now open: (field, ask, expect); None → unchanged
    ask: tuple[str, str, str] | None = None
    #: the open question's answer is free text, so a reply in two parts is joined rather than asked "which one?"
    free_text: bool = False
    #: outputs this node set this turn (e.g. {"selected_job": {...}})
    outputs: dict[str, Any] = dc_field(default_factory=dict)
    complete: bool = False
    exit_reason: str | None = None
    #: PII categories legitimately requested, so output guardrails allow them
    allowed_pii: set[str] = dc_field(default_factory=set)
    #: items shown verbatim (translate=False tools)
    verbatim: list[dict[str, Any]] = dc_field(default_factory=list)
    #: sent exactly as written, never rephrased: compliance and safety replies (opt-out, refusals, stop checks)
    fixed: bool = False


class Node[C: CapabilityConfig]:
    """Node API: ``async def act(ctx, config) -> NodeResult``."""

    config_model: ClassVar[type[CapabilityConfig]] = EmptyConfig
    timeout_s: ClassVar[float] = 60.0  # a node may make a model call (with provider retries) plus a tool call
    #: fields this node asks for (besides a FormFill's configured fields); used by the Intent Detector and `edit`
    fields: ClassVar[tuple[str, ...]] = ()
    #: the step's input is the user's questions (an analyst, a Q&A step): an in-scope enquiry on this step is routed to
    #: it, not counted as a digression. Off-topic messages still count.
    takes_questions: ClassVar[bool] = False

    async def act(self, ctx: TurnContext, config: C) -> NodeResult:
        raise NotImplementedError


@dataclass(frozen=True)
class NodeSpec:
    name: str
    cls: type[Node[Any]]
    config_model: type[CapabilityConfig]
    tools: tuple[str, ...] = ()
    produces: tuple[str, ...] = ()
    version: str = "1"
    description: str = ""
    #: repair attempts per stage: the default and the maximum. Empty: one "default" stage at the platform default
    repairs: Mapping[str, int] = dc_field(default_factory=dict)
    #: check the phrased reply before it is sent (respond/rewriter.py); False passes it through. A step may override it.
    verify: bool = True

    def schema_hash(self) -> str:
        schema = self.config_model.model_json_schema()
        extra = {**({"repairs": dict(self.repairs)} if self.repairs else {}), **({} if self.verify else {"verify": False})}
        return _schema_hash({**schema, **extra} if extra else schema)


# ---------------------------------------------------------------- guardrails


@dataclass
class GuardContext:
    step: str | None = None
    field: str | None = None
    allowed_pii: set[str] = dc_field(default_factory=set)
    language: str = "en"


@dataclass
class GuardResult:
    flagged: bool = False
    reason: str = ""
    redacted: str | None = None


class Guardrail:
    timeout_s: ClassVar[float] = 2.0

    async def check(self, text: str, ctx: GuardContext) -> GuardResult:
        raise NotImplementedError


@dataclass(frozen=True)
class GuardrailSpec:
    name: str
    cls: type[Guardrail]
    stage: GuardStage
    version: str = "1"
    description: str = ""

    def schema_hash(self) -> str:
        return _schema_hash({"stage": self.stage})


# ---------------------------------------------------------------- manifest


class ManifestEntry(BaseModel):
    name: str
    kind: Literal["tool", "action", "node", "guardrail", "preprocessor"]
    version: str
    schema_hash: str
    description: str = ""
    properties: dict[str, Any] = Field(default_factory=dict)


class Manifest(BaseModel):
    entries: list[ManifestEntry]

    def versions(self) -> dict[str, str]:
        return {f"{e.kind}:{e.name}": f"{e.version}@{e.schema_hash}" for e in self.entries}


# ---------------------------------------------------------------- registry


class RegistryError(Exception):
    pass


class Registry:
    ENTRY_POINT_GROUP = "keelchat.capabilities"

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._tenant_tools: dict[tuple[str, str], ToolSpec] = {}
        self._actions: dict[str, ActionSpec] = {}
        self._nodes: dict[str, NodeSpec] = {}
        self._guards: dict[str, GuardrailSpec] = {}
        self._pre: dict[str, PreProcessorSpec] = {}
        self._discovered = False

    # registration
    def add_tool(self, spec: ToolSpec) -> None:
        if spec.tenant is None:
            self._tools[spec.name] = spec
            return
        if spec.name in self._tools:
            raise RegistryError(f"{spec.name!r} is a platform tool")
        if (spec.tenant, spec.name) in self._tenant_tools:  # pinned policies may use it: registrations are immutable
            raise RegistryError(f"{spec.name!r} is already registered for {spec.tenant}; register a new name to change it")
        self._tenant_tools[(spec.tenant, spec.name)] = spec

    def remove_tool(self, name: str, tenant: str | None = None) -> None:
        if tenant is None:
            self._tools.pop(name, None)
        else:
            self._tenant_tools.pop((tenant, name), None)

    def add_action(self, spec: ActionSpec) -> None:
        self._actions[spec.name] = spec

    def add_node(self, spec: NodeSpec) -> None:
        self._nodes[spec.name] = spec

    def add_guardrail(self, spec: GuardrailSpec) -> None:
        self._guards[spec.name] = spec

    def add_preprocessor(self, spec: PreProcessorSpec) -> None:
        self._pre[spec.name] = spec

    # lookups
    def _visible(self, tenant: str | None) -> dict[str, ToolSpec]:
        """Every tool a tenant's agents can see: its own, then platform and MCP tools (which win on a name clash)."""
        own = {n: s for (t, n), s in self._tenant_tools.items() if t == tenant} if tenant is not None else {}
        return {**own, **self._tools}

    def has_tool(self, name: str, tenant: str | None = None) -> bool:
        return name in self._visible(tenant)

    def has_action(self, name: str) -> bool:
        return name in self._actions

    def has_node(self, name: str) -> bool:
        return name in self._nodes

    def has_guardrail(self, name: str) -> bool:
        return name in self._guards

    def tool(self, name: str, tenant: str | None = None) -> ToolSpec:
        spec = self._visible(tenant).get(name)
        if spec is None:
            raise RegistryError(f"unknown tool {name!r}; known: {self.tool_names(tenant)}")
        return spec

    def tenant_tools(self, tenant: str) -> list[ToolSpec]:
        return sorted((s for s in self._visible(tenant).values() if s.tenant == tenant), key=lambda s: s.name)

    def action(self, name: str) -> ActionSpec:
        try:
            return self._actions[name]
        except KeyError:
            raise RegistryError(f"unknown action {name!r}; known: {self.action_names()}") from None

    def node(self, name: str) -> NodeSpec:
        try:
            return self._nodes[name]
        except KeyError:
            raise RegistryError(f"unknown node {name!r}; known: {self.node_names()}") from None

    def guardrail(self, name: str) -> GuardrailSpec:
        try:
            return self._guards[name]
        except KeyError:
            raise RegistryError(f"unknown guardrail {name!r}; known: {self.guardrail_names()}") from None

    def preprocessor(self, name: str) -> PreProcessorSpec:
        try:
            return self._pre[name]
        except KeyError:
            raise RegistryError(f"unknown preprocessor {name!r}; known: {sorted(self._pre)}") from None

    def preprocessors(self) -> list[PreProcessorSpec]:
        return sorted(self._pre.values(), key=lambda p: (p.order, p.name))

    def tool_names(self, tenant: str | None = None) -> list[str]:
        return sorted(self._visible(tenant))

    def action_names(self) -> list[str]:
        return sorted(self._actions)

    def node_names(self) -> list[str]:
        return sorted(self._nodes)

    def guardrail_names(self) -> list[str]:
        return sorted(self._guards)

    def manifest(self, tenant: str | None = None) -> Manifest:
        """Platform and MCP tools, plus the given tenant's own tools."""
        tools = self._visible(tenant).values()
        entries = [
            ManifestEntry(
                name=t.name,
                kind="tool",
                version=t.version,
                schema_hash=t.schema_hash(),
                description=t.description,
                properties={
                    "translate": t.translate,
                    "timeout_s": t.timeout_s,
                    "retries": t.retries,
                    "side_effects": t.side_effects,
                    "source": t.source,
                    **({"tenant": t.tenant} if t.tenant else {}),
                    **({"dataset": t.dataset} if t.dataset else {}),
                    **({"repairs": dict(t.repairs)} if t.repairs else {}),
                },
            )
            for t in tools
        ]
        entries += [
            ManifestEntry(
                name=a.name,
                kind="action",
                version=a.version,
                schema_hash=a.schema_hash(),
                description=a.description,
                properties={"by": a.by, "timeout_s": a.timeout_s},
            )
            for a in self._actions.values()
        ]
        entries += [
            ManifestEntry(
                name=n.name,
                kind="node",
                version=n.version,
                schema_hash=n.schema_hash(),
                description=n.description,
                properties={
                    "tools": list(n.tools),
                    "produces": list(n.produces),
                    "fields": list(n.cls.fields),
                    **({"takes_questions": True} if n.cls.takes_questions else {}),
                    **({"repairs": dict(n.repairs)} if n.repairs else {}),
                    "verify": n.verify,
                },
            )
            for n in self._nodes.values()
        ]
        entries += [
            ManifestEntry(
                name=g.name, kind="guardrail", version=g.version, schema_hash=g.schema_hash(), description=g.description, properties={"stage": g.stage}
            )
            for g in self._guards.values()
        ]
        entries += [
            ManifestEntry(
                name=p.name, kind="preprocessor", version=p.version, schema_hash=p.schema_hash(), description=p.description, properties={"order": p.order}
            )
            for p in self._pre.values()
        ]
        return Manifest(entries=sorted(entries, key=lambda e: (e.kind, e.name)))

    def discover(self, force: bool = False) -> None:
        if self._discovered and not force:
            return
        self._discovered = True
        from keelchat.registry.discovery import discover_all

        discover_all(self)


REGISTRY = Registry()


# ---------------------------------------------------------------- decorators


def tool(
    name: str,
    *,
    translate: bool = True,
    timeout_s: float = 5.0,
    retries: int = 1,
    side_effects: SideEffects = "none",
    version: str = "1",
    dataset: DatasetKind | None = None,
    repairs: Mapping[str, int] | None = None,
) -> Callable[[ToolFn], ToolFn]:
    def deco(fn: ToolFn) -> ToolFn:
        hints = get_type_hints(fn)
        hint = hints.get("dataset")
        if (dataset is None) != (hint is None) or (hint is not None and hint is not Dataset and hint != Dataset | None):
            raise RegistryError(f"{name}: a dataset tool declares dataset=<kind> and takes `dataset: Dataset` (or `Dataset | None`)")
        if bool(repairs) != ("repairs" in hints):
            raise RegistryError(f"{name}: a tool with repairs=... takes `repairs: dict[str, int] | None = None`")
        REGISTRY.add_tool(
            ToolSpec(
                name=name,
                fn=fn,
                input_model=_input_model_from_signature(name, fn),
                output_schema=_output_schema(fn),
                translate=translate,
                timeout_s=timeout_s,
                retries=retries,
                side_effects=side_effects,
                version=version,
                description=inspect.getdoc(fn) or "",
                dataset=dataset,
                repairs=_check_repairs(name, repairs),
            )
        )
        return fn

    return deco


def action(name: str, *, by: Literal["candidate"] = "candidate", timeout_s: float = 10.0, version: str = "1") -> Callable[[ToolFn], ToolFn]:
    """A candidate-only action. Not a tool: the gateway rejects nodes and the planner; only the candidate's own action runs it."""

    def deco(fn: ToolFn) -> ToolFn:
        REGISTRY.add_action(
            ActionSpec(
                name=name,
                fn=fn,
                input_model=_input_model_from_signature(name, fn),
                output_schema=_output_schema(fn),
                by=by,
                timeout_s=timeout_s,
                version=version,
                description=inspect.getdoc(fn) or "",
            )
        )
        return fn

    return deco


def _check_repairs(name: str, repairs: Mapping[str, int] | None) -> dict[str, int]:
    out = dict(repairs or {})
    if any(not isinstance(n, int) or not 0 <= n <= 10 for n in out.values()):
        raise RegistryError(f"{name}: repairs are attempts per stage, 0..10; got {out}")
    return out


def node(
    name: str,
    *,
    tools: list[str] | None = None,
    produces: list[str] | None = None,
    version: str = "1",
    repairs: Mapping[str, int] | None = None,
    verify: bool = True,
) -> Callable[[type[Node[Any]]], type[Node[Any]]]:
    """`repairs`: attempts per stage at fixing the node's own model output, e.g. {"sql": 3, "code": 3}. Each value is
    the default and the maximum; a policy may lower it (limits.repairs, or `repairs:` on the step).
    `verify`: check this node's phrased replies before they are sent; a step may turn it off (`verify: false`)."""

    def deco(cls: type[Node[Any]]) -> type[Node[Any]]:
        REGISTRY.add_node(
            NodeSpec(
                name=name,
                cls=cls,
                config_model=cls.config_model,
                tools=tuple(tools or ()),
                produces=tuple(produces or ()),
                version=version,
                description=inspect.getdoc(cls) or "",
                repairs=_check_repairs(name, repairs),
                verify=verify,
            )
        )
        return cls

    return deco


def guardrail(name: str, *, stage: GuardStage, version: str = "1") -> Callable[[type[Guardrail]], type[Guardrail]]:
    def deco(cls: type[Guardrail]) -> type[Guardrail]:
        REGISTRY.add_guardrail(GuardrailSpec(name=name, cls=cls, stage=stage, version=version, description=inspect.getdoc(cls) or ""))
        return cls

    return deco


def preprocessor(name: str, *, order: int, version: str = "1") -> Callable[[type[PreProcessor]], type[PreProcessor]]:
    def deco(cls: type[PreProcessor]) -> type[PreProcessor]:
        cls.name = name
        REGISTRY.add_preprocessor(PreProcessorSpec(name=name, cls=cls, order=order, version=version, description=inspect.getdoc(cls) or ""))
        return cls

    return deco

"""The Agent Policy (agent.yaml): five core fields, three optional. Unknown keys rejected."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from yachat.policy.durations import Duration, parse_duration

Mode = Literal["flow", "planner", "adaptive"]
#: which model tier a step's work runs on: small (the classify model) or large (the generate model)
Tier = Literal["small", "large"]


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rule(PolicyModel):
    """A plain-language rule: when the condition holds for the user's message, the reply follows `then`. Matched each
    turn by the intent detector (a structured verdict); conflicts are settled in code by `overrides`."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    when: str = Field(min_length=1)
    then: str = Field(min_length=1)
    #: rules this one wins over when both match
    overrides: list[str] = Field(default_factory=list)
    #: a turn this rule matches is written with the large model and always checked before it is sent
    high_stakes: bool = False


class FlowStep(PolicyModel):
    """One flow entry: ``- job_search`` or ``- form_fill: {requires: [...], fields: [...]}``."""

    name: str
    requires: list[str] | None = None
    mode: Mode | None = None
    #: repair attempts for this step: one number for every stage, or per stage ({sql: 1}); never above the node's maximum
    repairs: int | dict[str, int] | None = None
    #: check this step's phrased replies before they are sent; None: the node's own setting
    verify: bool | None = None
    #: rules that apply only while this step is asking
    rules: list[Rule] = Field(default_factory=list)
    #: the model tier this step's work runs on; None: each job keeps its usual tier
    model: Tier | None = None
    #: every reply of this step is written with the large model and checked before it is sent
    high_stakes: bool = False
    #: the reply is planned before it is written (what the user needs, what is unknown, the rules it must follow)
    think: bool = False
    settings: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def parse(cls, raw: Any) -> FlowStep:
        if isinstance(raw, str):
            return cls(name=raw)
        if isinstance(raw, dict) and len(raw) == 1:
            name, body = next(iter(raw.items()))
            body = dict(body or {})
            if not isinstance(name, str):
                raise ValueError("step name must be a string")
            return cls(
                name=name,
                requires=body.pop("requires", None),
                mode=body.pop("mode", None),
                repairs=body.pop("repairs", None),
                verify=body.pop("verify", None),
                rules=body.pop("rules", None) or [],
                model=body.pop("model", None),
                high_stakes=body.pop("high_stakes", False),
                think=body.pop("think", False),
                settings=body,
            )
        raise ValueError("a flow step is a name or a single-key mapping {name: settings}")


class MemoryField(PolicyModel):
    """A typed memory field: `field` plus either `extract` (instruction) or `from` (a validated form answer)."""

    field: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    extract: str | None = None
    from_: str | None = Field(default=None, alias="from", pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
    type: Any = "string"
    half_life: timedelta | None = None
    sensitive: bool | None = None
    confirm_on_reuse: bool = True

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @field_validator("half_life", mode="before")
    @classmethod
    def _dur(cls, v: Any) -> Any:
        return None if v is None else parse_duration(v)

    @field_validator("type")
    @classmethod
    def _type(cls, v: Any) -> Any:
        from yachat.policy.memory_types import type_to_schema

        type_to_schema(v)
        return v

    @model_validator(mode="after")
    def _one_source(self) -> MemoryField:
        if bool(self.extract) == bool(self.from_):
            raise ValueError("a memory field needs exactly one of `extract` or `from`")
        return self

    @classmethod
    def parse(cls, raw: Any) -> MemoryField:
        if isinstance(raw, str):
            return cls(field=raw, extract=f"The user's {raw.replace('_', ' ')}, when they state it")
        if isinstance(raw, dict):
            return cls.model_validate(raw)
        raise ValueError("a memory item is a field name or a mapping with `field` and `extract` or `from`")


class GuardrailsSpec(PolicyModel):
    """Named checks from the registry. The base set (prompt_injection, pii) always runs even if omitted."""

    input: list[str] | None = None
    output: list[str] | None = None


class Limits(PolicyModel):
    """Per-question re-asks, consecutive digressions, reopens per step, planner tool calls per turn, on-step enquiries per
    step, repair attempts per node stage."""

    reasks: int | None = Field(default=None, ge=1)
    digressions: int | None = Field(default=None, ge=1)
    reopens: int | None = Field(default=None, ge=0)
    planner_calls: int | None = Field(default=None, ge=1)
    on_step_enquiries: int | None = Field(default=None, ge=1)
    #: repair attempts per stage for every node in this policy, capped at each node's maximum
    repairs: int | None = Field(default=None, ge=0)


#: when a conversation is passed to a person: the user asks for one, or the flow reaches one of these dead ends
HandoffTrigger = Literal["user_request", "max_reasks", "disengaged", "tool_unavailable"]
#: friendly small talk ("how's your day?"): off (counted as wandering off), brief (one friendly line, then back to the
#: task), warm (also picks up what they mentioned)
SmallTalk = Literal["off", "brief", "warm"]
#: the arguments every handoff notify tool takes (a follow-up message on an open ticket carries `ticket_id`)
HANDOFF_ARGS = ("reason", "session_id", "agent", "fields", "transcript", "ticket_id")


class HandoffSpec(PolicyModel):
    """Pass the conversation to a person: it lands in the platform's handoff inbox; `notify` also sends it to a
    helpdesk through a registered write tool (e.g. the generic `handoff_webhook`)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    notify: str | None = None
    when: list[HandoffTrigger] = Field(default_factory=lambda: list[HandoffTrigger](["user_request"]), min_length=1)
    #: with no callback by then, the assistant takes the conversation back at the user's next message; None: it waits
    timeout: Duration | None = None

    @field_validator("when")
    @classmethod
    def _once(cls, v: list[HandoffTrigger]) -> list[HandoffTrigger]:
        if dupes := sorted({w for w in v if v.count(w) > 1}):
            raise ValueError(f"listed twice: {', '.join(dupes)}")
        return v


class AgentPolicy(PolicyModel):
    agent: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    charter: str = Field(min_length=1)
    languages: list[str] = Field(default_factory=lambda: ["en"], min_length=1)
    tools: list[str] = Field(default_factory=list)
    flow: list[FlowStep] | None = None
    guardrails: GuardrailsSpec = Field(default_factory=GuardrailsSpec)
    limits: Limits = Field(default_factory=Limits)
    memory: list[MemoryField] = Field(default_factory=list)
    mode: Mode | None = None
    handoff: HandoffSpec | None = None
    #: rules that apply on every turn
    rules: list[Rule] = Field(default_factory=list)
    small_talk: SmallTalk = "off"

    @field_validator("flow", mode="before")
    @classmethod
    def _parse_flow(cls, v: Any) -> Any:
        if v is None:
            return None
        if not isinstance(v, list):
            raise ValueError("flow must be a list of steps")
        return [FlowStep.parse(s) if not isinstance(s, FlowStep) else s for s in v]

    @field_validator("memory", mode="before")
    @classmethod
    def _parse_memory(cls, v: Any) -> Any:
        if not isinstance(v, list):
            raise ValueError("memory must be a list of items")
        return [MemoryField.parse(m) if not isinstance(m, MemoryField) else m for m in v]

    @model_validator(mode="after")
    def _defaults(self) -> AgentPolicy:
        return self

    @property
    def effective_mode(self) -> Mode:
        """`flow` when a flow exists, `planner` when none; `adaptive` is opt-in."""
        return self.mode or ("flow" if self.flow else "planner")

    def step(self, name: str) -> FlowStep:
        for s in self.flow or []:
            if s.name == name:
                return s
        raise KeyError(name)

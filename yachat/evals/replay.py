"""Replay sets: scripted conversations and personas the model plays, run on the simulator's engine (real model, fake
tools), with expectations checked in code."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from yachat.policy.compiler import CompiledPolicy, build
from yachat.runtime.llm import LLMClient, ModelConfig
from yachat.runtime.llm_factory import llm_from_env
from yachat.sim.harness import TurnLog, run_conversation, run_persona, sim_runtime
from yachat.testing.fakes import FakeTools


class Expect(BaseModel):
    """What must hold when the conversation ends. Unset keys are not checked; dict values check only the keys given."""

    model_config = ConfigDict(extra="forbid")

    exit_reason: str | None = None
    completed: bool | None = None
    steps: dict[str, str] | None = None
    fields: dict[str, str] | None = None
    digressions: int | None = None
    nudges: int | None = None
    reasks: dict[str, int] | None = None
    language: str | None = None
    #: turn number (from 1) → the rules that must match on it, exactly; `[]` means none may
    rules: dict[int, list[str]] | None = None
    #: rules that must match on some turn / on no turn (when the turn is not known, e.g. a played persona)
    rules_fired: list[str] | None = None
    rules_not_fired: list[str] | None = None


class ReplayCase(BaseModel):
    """`messages` replays a script; `persona` has the model play that user for up to `max_turns`."""

    model_config = ConfigDict(extra="forbid")

    name: str
    messages: list[str] = Field(default_factory=list)
    persona: str = ""
    max_turns: int = Field(default=12, ge=1, le=20)
    channel: str = "web"
    user_id: str | None = None
    expected_tools: list[str] = Field(default_factory=list)
    expect: Expect = Field(default_factory=Expect)
    chatbot_role: str = ""
    #: why the case exists, e.g. what a generated case covers or the check it failed
    note: str = ""

    @model_validator(mode="after")
    def _one_kind(self) -> ReplayCase:
        if bool(self.messages) == bool(self.persona):
            raise ValueError("give either messages or persona")
        return self


class Fixtures(BaseModel):
    """What the fake tools return in this eval set; unset, the built-in samples."""

    model_config = ConfigDict(extra="forbid")

    #: knowledge_search results: {title, text, source}
    passages: list[dict[str, str]] | None = None
    #: job_search results
    jobs: list[dict[str, Any]] | None = None

    def tools(self) -> FakeTools:
        return FakeTools(jobs=self.jobs, passages=self.passages)


class ReplaySet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixtures: Fixtures = Field(default_factory=Fixtures)
    cases: list[ReplayCase]


class Outcome(BaseModel):
    """The session when the conversation ended: what `expect` is checked against (same keys)."""

    exit_reason: str | None
    completed: bool
    steps: dict[str, str]
    fields: dict[str, str]
    digressions: int
    nudges: int
    reasks: dict[str, int]
    language: str | None


@dataclass
class ReplayResult:
    case: ReplayCase
    turns: list[TurnLog]
    state: Outcome

    @property
    def expectation_failures(self) -> list[str]:
        out = []
        for k in self.case.expect.model_fields_set - {"rules", "rules_fired", "rules_not_fired"}:
            want, got = getattr(self.case.expect, k), getattr(self.state, k)
            ok = all(got.get(kk) == vv for kk, vv in want.items()) if isinstance(want, dict) and isinstance(got, dict) else got == want
            if not ok:
                out.append(f"{k}: expected {want!r}, got {got!r}")
        for n, want_rules in (self.case.expect.rules or {}).items():
            got_rules = self.turns[n - 1].rules if 0 < n <= len(self.turns) else None
            if got_rules is None or sorted(got_rules) != sorted(want_rules):
                out.append(f"rules on turn {n}: expected {want_rules!r}, got {got_rules!r}")
        fired = {n for t in self.turns for n in t.rules}
        if missing := [n for n in self.case.expect.rules_fired or [] if n not in fired]:
            out.append(f"rules never matched: {missing}")
        if wrong := [n for n in self.case.expect.rules_not_fired or [] if n in fired]:
            out.append(f"rules matched but should not have: {wrong}")
        if self.case.expected_tools:
            used = {t for turn in self.turns for t in turn.tools}
            if missing := [t for t in self.case.expected_tools if t not in used]:
                out.append(f"expected tools not called: {missing}")
        return out

    def to_conversational_test_case(self, role: str = "") -> Any:
        """DeepEval ConversationalTestCase (imported lazily: the harness runs without deepeval installed). `role` is the
        chatbot role when the case gives none (the policy's charter)."""
        from deepeval.test_case import ConversationalTestCase, Turn

        turns: list[Any] = []
        for t in self.turns:
            turns.append(Turn(role="user", content=t.user))
            turns.append(Turn(role="assistant", content=t.agent, retrieval_context=list[Any](t.tool_outputs) or None))
        return ConversationalTestCase(turns=turns, chatbot_role=self.case.chatbot_role or role or None, name=self.case.name)

    def to_llm_test_cases(self) -> list[Any]:
        """One single-turn LLMTestCase per agent turn, with that turn's tool outputs as retrieval_context."""
        from deepeval.test_case import LLMTestCase

        return [
            LLMTestCase(input=t.user, actual_output=t.agent, retrieval_context=list[Any](t.tool_outputs or [""]), name=f"{self.case.name}#{i}")
            for i, t in enumerate(self.turns)
        ]


def load_set(path: str | Path) -> ReplaySet:
    return ReplaySet.model_validate(yaml.safe_load(Path(path).read_text()))


#: cases `yachat evals generate` wrote, next to the hand-written replay.yaml
GENERATED = "generated.yaml"


def load_agent_set(agent_dir: str | Path) -> ReplaySet:
    """An eval set folder's cases: replay.yaml (fixtures and hand-written cases) plus generated.yaml, either optional."""
    d = Path(agent_dir)
    found = [load_set(p) for p in (d / "replay.yaml", d / GENERATED) if p.is_file()]
    if not found:
        raise FileNotFoundError(f"{d}: no replay.yaml or {GENERATED}")
    cases = [c for s in found for c in s.cases]
    if dupes := sorted({c.name for c in cases if [x.name for x in cases].count(c.name) > 1}):
        raise ValueError(f"{d}: case names used twice across replay.yaml and {GENERATED}: {dupes}")
    return ReplaySet(fixtures=found[0].fixtures, cases=cases)


def load_cases(path: str | Path) -> list[ReplayCase]:
    return load_set(path).cases


#: replay cases run at once (each its own runtime); the provider's rate limit is the ceiling
PARALLEL = 3


async def run_case(policy: CompiledPolicy | str, case: ReplayCase, llm: LLMClient | None = None, fixtures: Fixtures | None = None) -> ReplayResult:
    policy = build(policy) if isinstance(policy, str) else policy
    rt = sim_runtime(llm or llm_from_env(), ModelConfig.from_env(), (fixtures or Fixtures()).tools())
    await rt.register_policy(policy)
    if case.persona:
        report = await run_persona(rt, policy, case.name, case.persona, case.max_turns, case.channel, case.user_id)
    else:
        report = await run_conversation(rt, policy, case.name, case.messages, case.channel, case.user_id)
    await rt.drain()
    st = await rt.session_state(report.session_id)
    outcome = Outcome(
        exit_reason=st.exit_reason,
        completed=st.completed,
        steps={k: v.status for k, v in st.steps.items()},
        fields=dict(st.fields),
        digressions=st.digressions,
        nudges=st.nudges,
        reasks=dict(st.reasks),
        language=st.language,
    )
    return ReplayResult(case, report.turn_log, outcome)


async def run_set(policy: CompiledPolicy, replay: ReplaySet, llm: LLMClient | None = None, parallel: int = PARALLEL) -> list[ReplayResult]:
    """Every case, up to `parallel` at once, in the set's order."""
    gate = asyncio.Semaphore(parallel)

    async def one(c: ReplayCase) -> ReplayResult:
        async with gate:
            return await run_case(policy, c, llm, replay.fixtures)

    return list(await asyncio.gather(*(one(c) for c in replay.cases)))


async def run_replay_set(policy_path: str | Path, cases_path: str | Path, llm: LLMClient | None = None) -> list[ReplayResult]:
    return await run_set(build(Path(policy_path).read_text()), load_set(cases_path), llm)

"""Replay sets: scripted multi-turn conversations run against the runtime (real model, fake tools) → DeepEval ConversationalTestCase."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

import yaml

from yachat.policy.compiler import build
from yachat.runtime.context import InboundMessage
from yachat.runtime.llm import LLMClient, ModelConfig
from yachat.runtime.llm_factory import llm_from_env
from yachat.runtime.runtime import Runtime
from yachat.testing.fakes import FakeTools


@dataclass
class ReplayCase:
    name: str
    messages: list[str]
    channel: str = "web"
    user_id: str | None = None
    expected_tools: list[str] = dc_field(default_factory=list)
    expect: dict[str, Any] = dc_field(default_factory=dict)  # e.g. {"exit_reason": "disengaged", "completed": true}
    chatbot_role: str = ""


@dataclass
class ReplayTurn:
    user: str
    agent: str
    tools: list[str]
    tool_outputs: list[str]
    route: str | None
    llm_calls: int


@dataclass
class ReplayResult:
    case: ReplayCase
    turns: list[ReplayTurn]
    state: dict[str, Any]

    @property
    def expectation_failures(self) -> list[str]:
        out = []
        for k, v in self.case.expect.items():
            actual = self.state.get(k)
            ok = all(actual.get(kk) == vv for kk, vv in v.items()) if isinstance(v, dict) and isinstance(actual, dict) else actual == v
            if not ok:
                out.append(f"{k}: expected {v!r}, got {actual!r}")
        if self.case.expected_tools:
            used = [t for turn in self.turns for t in turn.tools]
            missing = [t for t in self.case.expected_tools if t not in used]
            if missing:
                out.append(f"expected tools not called: {missing}")
        return out

    def to_conversational_test_case(self) -> Any:
        """DeepEval ConversationalTestCase (imported lazily so the harness works without deepeval installed)."""
        from deepeval.test_case import ConversationalTestCase, Turn

        turns: list[Any] = []
        for t in self.turns:
            turns.append(Turn(role="user", content=t.user))
            turns.append(Turn(role="assistant", content=t.agent, retrieval_context=t.tool_outputs or None))
        return ConversationalTestCase(turns=turns, chatbot_role=self.case.chatbot_role or None, name=self.case.name)

    def to_llm_test_cases(self) -> list[Any]:
        """One single-turn LLMTestCase per agent turn, with that turn's tool outputs as retrieval_context (Faithfulness)."""
        from deepeval.test_case import LLMTestCase

        return [
            LLMTestCase(input=t.user, actual_output=t.agent, retrieval_context=t.tool_outputs or [""], tools_called=None, name=f"{self.case.name}#{i}")
            for i, t in enumerate(self.turns)
        ]


def load_cases(path: str | Path) -> list[ReplayCase]:
    data = yaml.safe_load(Path(path).read_text())
    return [
        ReplayCase(
            name=c["name"],
            messages=list(c["messages"]),
            channel=c.get("channel", "web"),
            user_id=c.get("user_id"),
            expected_tools=list(c.get("expected_tools", [])),
            expect=dict(c.get("expect", {})),
            chatbot_role=c.get("chatbot_role", ""),
        )
        for c in data["cases"]
    ]


class _RecordingTools(FakeTools):
    """FakeTools that also records tool output per turn so Faithfulness can use it as retrieval_context."""

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.turn_log: list[tuple[str, str]] = []

    async def _gate(self, name: str, args: dict[str, Any]) -> None:
        await super()._gate(name, args)

    def impls(self) -> dict[str, Any]:
        base = super().impls()

        def wrap(name: str, fn: Any) -> Any:
            async def inner(**kwargs: Any) -> Any:
                out = await fn(**kwargs)
                self.turn_log.append((name, str(out)[:2000]))
                return out

            return inner

        return {n: wrap(n, f) for n, f in base.items()}


async def run_case(policy_text: str, case: ReplayCase, llm: LLMClient | None = None) -> ReplayResult:
    policy = build(policy_text)
    tools = _RecordingTools()
    rt = Runtime(llm or llm_from_env(), tools.impls(), models=ModelConfig.from_env())
    await rt.register_policy(policy)
    user = case.user_id or f"replay-{case.name}"
    s = await rt.start_session(policy.agent, user, case.channel)
    turns: list[ReplayTurn] = []
    for i, msg in enumerate(case.messages):
        tools.turn_log.clear()
        r = await rt.turn(s.id, InboundMessage(text=msg, channel=case.channel, user_id=user, channel_message_id=f"{case.name}-{i}"))
        turns.append(ReplayTurn(msg, r.outbound.text, [n for n, _ in tools.turn_log], [o for _, o in tools.turn_log], r.route, r.llm_calls))
        if r.state.finished:
            break
    await rt.drain()
    st = await rt.session_state(s.id)
    state = {
        "exit_reason": st.exit_reason,
        "completed": st.completed,
        "steps": {k: v.status for k, v in st.steps.items()},
        "fields": st.fields,
        "digressions": st.digressions,
        "nudges": st.nudges,
        "reasks": st.reasks,
        "language": st.language,
    }
    return ReplayResult(case, turns, state)


async def run_replay_set(policy_path: str | Path, cases_path: str | Path, llm: LLMClient | None = None) -> list[ReplayResult]:
    text = Path(policy_path).read_text()
    return [await run_case(text, c, llm) for c in load_cases(cases_path)]

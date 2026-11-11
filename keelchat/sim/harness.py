"""Simulated-user harness: scripted conversations, or a model playing a persona, against a policy with fake tools.
Reports exit reasons, re-ask rates, digressions, handoffs and each conversation's decision log."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from keelchat.policy.compiler import CompiledPolicy, build
from keelchat.runtime.audit import AuditSummary, audit_db, summarize
from keelchat.runtime.context import InboundMessage
from keelchat.runtime.decide.tool_gateway import to_plain
from keelchat.runtime.llm import LLMClient, LLMRequest, ModelConfig
from keelchat.runtime.llm_factory import llm_from_env
from keelchat.runtime.runtime import Runtime
from keelchat.state.models import AuditEvent
from keelchat.testing.fakes import FakeTools


@dataclass
class TurnLog:
    """One turn as the evals score it: what the tools returned (the retrieved context) and which rules matched."""

    user: str
    agent: str
    tools: list[str] = dc_field(default_factory=list)
    tool_outputs: list[str] = dc_field(default_factory=list)
    rules: list[str] = dc_field(default_factory=list)
    route: str | None = None
    llm_calls: int = 0
    #: stages that failed on this turn (the reply fell back), e.g. `intent_detector: LLMError`
    errors: list[str] = dc_field(default_factory=list)
    #: form fields re-asked on this turn (their count went up; the session clears it once the field is answered)
    reasked: list[str] = dc_field(default_factory=list)
    #: form fields followed up on this turn (a thin answer with `curious` on)
    probed: list[str] = dc_field(default_factory=list)


@dataclass
class ConversationReport:
    name: str
    turns: int
    exit_reason: str | None
    completed: bool
    reasks: int
    max_digressions: int
    nudges: int
    llm_calls: int
    tool_calls: int
    transcript: list[dict[str, str]] = dc_field(default_factory=list)
    #: the persona the model played (empty for a scripted conversation)
    persona: str = ""
    #: the platform's decisions in this conversation, oldest first (the audit log of the simulated session)
    decisions: list[dict[str, Any]] = dc_field(default_factory=list)
    #: each turn's tools, tool output and matched rules
    turn_log: list[TurnLog] = dc_field(default_factory=list)
    session_id: str = ""


@dataclass
class SimReport:
    conversations: list[ConversationReport]
    summary: AuditSummary | None = None

    @property
    def exit_reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.conversations:
            k = c.exit_reason or "open"
            out[k] = out.get(k, 0) + 1
        return out

    @property
    def reask_rate(self) -> float:
        asked = sum(c.turns for c in self.conversations) or 1
        return sum(c.reasks for c in self.conversations) / asked

    def to_dict(self) -> dict[str, Any]:
        return {
            "exit_reasons": self.exit_reasons,
            "reask_rate": round(self.reask_rate, 3),
            "avg_llm_calls_per_turn": round(sum(c.llm_calls for c in self.conversations) / (sum(c.turns for c in self.conversations) or 1), 2),
            "conversations": [asdict(c) for c in self.conversations],
            **({"summary": self.summary.model_dump(mode="json")} if self.summary else {}),
        }


#: most turns a model-played user takes before the conversation is cut off
MAX_PERSONA_TURNS = 20

PERSONA_SYSTEM = """You play a user chatting with a company's assistant, to test it. Stay in character.
Persona: {persona}
What the assistant is for: {charter}
Write only the user's next message: short and natural, as this person would type it in a {channel} chat. Never
mention that you are testing or role-playing. Set done to true when this person would stop after this message (or
with an empty message, now): their goal is reached, they were handed to a person, or they would give up."""

PERSONA_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"message": {"type": "string"}, "done": {"type": "boolean"}},
    "required": ["message", "done"],
    "additionalProperties": False,
}


async def persona_message(
    llm: LLMClient, models: ModelConfig, policy: CompiledPolicy, persona: str, channel: str, transcript: list[dict[str, str]]
) -> tuple[str | None, bool]:
    """The persona's next message (None: nothing more to say) and whether it stops after it. The agent's replies reach
    the model as the other party."""
    messages: list[dict[str, str]] = [{"role": "user", "content": "(The chat opens. Write your first message.)"}]
    for t in transcript:
        messages += [{"role": "assistant", "content": t["user"]}, {"role": "user", "content": t["agent"] or "(no reply)"}]
    system = PERSONA_SYSTEM.format(persona=persona, charter=policy.charter.strip(), channel=channel)
    out = await llm.structured(LLMRequest("sim_persona", models.generate, system, messages, max_tokens=300), PERSONA_SCHEMA)
    text = str(out.get("message") or "").strip()
    return text or None, bool(out.get("done"))


#: the current turn's (tool, output) pairs; set per turn so conversations running at once keep their own
_TOOL_LOG: ContextVar[list[tuple[str, str]] | None] = ContextVar("sim_tool_log", default=None)
#: longest tool output kept per call
TOOL_OUTPUT_CHARS = 2000


def _recorded(name: str, fn: Callable[..., Any]) -> Callable[..., Any]:
    async def call(**kwargs: Any) -> Any:
        out = await fn(**kwargs)
        if (log := _TOOL_LOG.get()) is not None:
            log.append((name, json.dumps(to_plain(out), ensure_ascii=False, default=str)[:TOOL_OUTPUT_CHARS]))  # as the gateway hands it on
        return out

    return call


async def _drive(
    rt: Runtime, policy: CompiledPolicy, name: str, channel: str, next_message: Any, persona: str = "", user_id: str | None = None
) -> ConversationReport:
    user = user_id or f"sim-{name}"
    s = await rt.start_session(policy.agent, user, channel, resume=False, tenant=policy.tenant)
    transcript: list[dict[str, str]] = []
    turn_log: list[TurnLog] = []
    digressions_seen, llm, tools, i = 0, 0, 0, 0
    while (msg := await next_message(i, transcript)) is not None:
        calls: list[tuple[str, str]] = []
        prev = await rt.session_state(s.id)
        before, probes_before = dict(prev.reasks), set(prev.data.get("probes", {}))
        token = _TOOL_LOG.set(calls)
        try:
            last = await rt.turn(s.id, InboundMessage(text=msg, channel=channel, user_id=user, channel_message_id=f"{name}-{i}"))
        finally:
            _TOOL_LOG.reset(token)
        transcript.append({"user": msg, "agent": last.outbound.text})
        names, outputs = [n for n, _ in calls], [o for _, o in calls]
        reasked = [f for f, n in last.state.reasks.items() if n > before.get(f, 0)]
        probed = [f for f in last.state.data.get("probes", {}) if f not in probes_before]
        turn_log.append(TurnLog(msg, last.outbound.text, names, outputs, list(last.rules), last.route, last.llm_calls, list(last.errors), reasked, probed))
        digressions_seen = max(digressions_seen, last.state.digressions)
        llm += last.llm_calls
        tools += last.tool_calls
        i += 1
        if last.state.finished:
            break
    st = await rt.session_state(s.id)
    events = list(reversed(await rt.audit.query(policy.tenant, session_id=s.id, limit=1000)))
    return ConversationReport(
        name,
        len(transcript),
        st.exit_reason,
        st.completed,
        sum(st.reasks.values()),
        digressions_seen,
        st.nudges,
        llm,
        tools,
        transcript,
        persona=persona,
        decisions=[_decision(e) for e in events],
        turn_log=turn_log,
        session_id=s.id,
    )


def _decision(e: AuditEvent) -> dict[str, Any]:
    return e.model_dump(mode="json", include={"kind", "decision", "reason", "step", "field", "ok", "ref"})


async def run_conversation(
    rt: Runtime, policy: CompiledPolicy, name: str, messages: list[str], channel: str = "web", user_id: str | None = None
) -> ConversationReport:
    async def scripted(i: int, _: list[dict[str, str]]) -> str | None:
        return messages[i] if i < len(messages) else None

    return await _drive(rt, policy, name, channel, scripted, user_id=user_id)


async def run_persona(
    rt: Runtime, policy: CompiledPolicy, name: str, persona: str, max_turns: int = 12, channel: str = "web", user_id: str | None = None
) -> ConversationReport:
    """The configured model plays the user, turn by turn, until the persona stops or `max_turns` is reached."""
    cap, stopped = min(max_turns, MAX_PERSONA_TURNS), False

    async def played(i: int, transcript: list[dict[str, str]]) -> str | None:
        nonlocal stopped
        if stopped or i >= cap:
            return None
        text, stopped = await persona_message(rt.llm, rt.models, policy, persona, channel, transcript)
        return text

    return await _drive(rt, policy, name, channel, played, persona=persona, user_id=user_id)


def sim_runtime(llm: LLMClient, models: ModelConfig, fakes: FakeTools | None = None, **stores: Any) -> Runtime:
    """A runtime of its own: fake tools (nothing is submitted or sent, each call's output is recorded per turn), its own
    sessions, memory, inbox and audit log."""
    tools = {n: _recorded(n, f) for n, f in (fakes or FakeTools()).impls().items()}
    return Runtime(llm, tools, models=models, **stores)


async def simulate(rt: Runtime, policy: CompiledPolicy, conversations: list[dict[str, Any]], parallel: int = 3) -> SimReport:
    """Each entry is scripted (`messages`) or played by the model (`persona`, optional `max_turns`); up to `parallel`
    run at once."""
    gate = asyncio.Semaphore(parallel)

    async def one(c: dict[str, Any]) -> ConversationReport:
        channel = c.get("channel", "web")
        async with gate:
            if c.get("persona"):
                return await run_persona(rt, policy, c["name"], c["persona"], int(c.get("max_turns", 12)), channel)
            return await run_conversation(rt, policy, c["name"], list(c["messages"]), channel)

    reports = list(await asyncio.gather(*(one(c) for c in conversations)))
    await rt.drain()
    db = await audit_db(rt.audit, policy.tenant, datetime(1970, 1, 1, tzinfo=UTC), agent=policy.agent)
    try:
        return SimReport(reports, summarize(db, None, None))
    finally:
        db.close()


async def run_script(policy_text: str, script: dict[str, Any], llm: LLMClient | None = None) -> SimReport:
    policy = build(policy_text)
    rt = sim_runtime(llm or llm_from_env(), ModelConfig.from_env())
    await rt.register_policy(policy)
    return await simulate(rt, policy, list(script["conversations"]))


async def run_script_file(policy_path: str, script_path: str) -> SimReport:
    return await run_script(Path(policy_path).read_text(), yaml.safe_load(Path(script_path).read_text()))

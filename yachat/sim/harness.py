"""Simulated-user harness: scripted conversations, or a model playing a persona, against a policy with fake tools.
Reports exit reasons, re-ask rates, digressions, handoffs and each conversation's decision log."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from dataclasses import field as dc_field
from pathlib import Path
from typing import Any

import yaml

from yachat.policy.compiler import CompiledPolicy, build
from yachat.runtime.audit import AuditSummary, summarize
from yachat.runtime.context import InboundMessage
from yachat.runtime.llm import LLMClient, LLMRequest, ModelConfig
from yachat.runtime.llm_factory import llm_from_env
from yachat.runtime.runtime import Runtime
from yachat.state.models import AuditEvent
from yachat.testing.fakes import FakeTools


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


async def _drive(rt: Runtime, policy: CompiledPolicy, name: str, channel: str, next_message: Any, persona: str = "") -> ConversationReport:
    s = await rt.start_session(policy.agent, f"sim-{name}", channel, resume=False, tenant=policy.tenant)
    transcript: list[dict[str, str]] = []
    digressions_seen, llm, tools, i = 0, 0, 0, 0
    while (msg := await next_message(i, transcript)) is not None:
        last = await rt.turn(s.id, InboundMessage(text=msg, channel=channel, user_id=f"sim-{name}", channel_message_id=f"{name}-{i}"))
        transcript.append({"user": msg, "agent": last.outbound.text})
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
    )


def _decision(e: AuditEvent) -> dict[str, Any]:
    return e.model_dump(mode="json", include={"kind", "decision", "reason", "step", "field", "ok", "ref"})


async def run_conversation(rt: Runtime, policy: CompiledPolicy, name: str, messages: list[str], channel: str = "web") -> ConversationReport:
    async def scripted(i: int, _: list[dict[str, str]]) -> str | None:
        return messages[i] if i < len(messages) else None

    return await _drive(rt, policy, name, channel, scripted)


async def run_persona(rt: Runtime, policy: CompiledPolicy, name: str, persona: str, max_turns: int = 12, channel: str = "web") -> ConversationReport:
    """The configured model plays the user, turn by turn, until the persona stops or `max_turns` is reached."""
    cap, stopped = min(max_turns, MAX_PERSONA_TURNS), False

    async def played(i: int, transcript: list[dict[str, str]]) -> str | None:
        nonlocal stopped
        if stopped or i >= cap:
            return None
        text, stopped = await persona_message(rt.llm, rt.models, policy, persona, channel, transcript)
        return text

    return await _drive(rt, policy, name, channel, played, persona=persona)


def sim_runtime(llm: LLMClient, models: ModelConfig, **stores: Any) -> Runtime:
    """A runtime of its own: fake tools (nothing is submitted or sent), its own sessions, memory, inbox and audit log."""
    return Runtime(llm, FakeTools().impls(), models=models, **stores)


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
    events = await rt.audit.query(policy.tenant, agent=policy.agent, limit=100_000)
    return SimReport(reports, summarize(events, None, None))


async def run_script(policy_text: str, script: dict[str, Any], llm: LLMClient | None = None) -> SimReport:
    policy = build(policy_text)
    rt = sim_runtime(llm or llm_from_env(), ModelConfig.from_env())
    await rt.register_policy(policy)
    return await simulate(rt, policy, list(script["conversations"]))


async def run_script_file(policy_path: str, script_path: str) -> SimReport:
    return await run_script(Path(policy_path).read_text(), yaml.safe_load(Path(script_path).read_text()))

"""TurnContext: the only thing stages share."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING, Any

from yachat.policy.compiler import CompiledPolicy
from yachat.registry.core import GuardContext, NodeResult
from yachat.state.models import AuditEvent, AuditKind, MemoryRecord, SessionState, new_id
from yachat.state.store import HandoffStore, MemoryStore, ResponseStore

if TYPE_CHECKING:  # pragma: no cover
    from yachat.logging import Metrics
    from yachat.runtime.decide.tool_gateway import ActionRunner, ToolGateway
    from yachat.runtime.llm import LLMClient, ModelConfig


@dataclass
class InboundMessage:
    text: str
    channel: str
    user_id: str
    channel_message_id: str | None = None
    metadata: dict[str, Any] = dc_field(default_factory=dict)
    #: a candidate-only action the channel passed through (e.g. a submit button)
    action: str | None = None


@dataclass
class OutboundMessage:
    """Channel-neutral: text plus structured items. Knows no channel limits."""

    text: str
    language: str = "en"
    items: list[dict[str, Any]] = dc_field(default_factory=list)
    end_of_conversation: bool = False
    exit_reason: str | None = None


@dataclass
class IntentItem:
    intent: str  # new_answer | edit | enquiry | off_topic
    fields: dict[str, str] = dc_field(default_factory=dict)
    edited_field: str | None = None
    needs_multi_step: bool = False
    on_step: bool = False
    text: str = ""


@dataclass
class AnswerVerdict:
    """Summary of the Intent Detector's list of intents for this message (items processed edits → answers → enquiries)."""

    verdict: str  # answered | digressed
    intent: str = "new_answer"  # dominant intent: edit > new_answer > enquiry > off_topic
    fields: dict[str, str] = dc_field(default_factory=dict)
    edited_field: str | None = None
    items: list[IntentItem] = dc_field(default_factory=list)
    #: the enquiry part of the message (Knowledge answers this, not the whole message)
    enquiry_text: str = ""
    #: a field given twice with different values → ask one clarifying question
    conflict: tuple[str, list[str]] | None = None
    #: every enquiry in the message is about the current step itself (never counted as a digression)
    on_step: bool = False
    needs_multi_step: bool = False
    clear_outputs: list[str] = dc_field(default_factory=list)
    in_scope: bool = True
    reason: str = ""
    skipped: bool = False
    #: the user asks to talk to a person (asked only when the policy hands off on user_request)
    wants_human: bool = False
    #: the user wants the conversation to stop (confirmed with them before it ends)
    wants_to_stop: bool = False


@dataclass
class StageTrace:
    stage: str
    ms: float
    skipped: bool = False
    note: str = ""


@dataclass
class ChannelMessage:
    text: str
    payload: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class Services:
    llm: LLMClient
    models: ModelConfig
    gateway: ToolGateway
    metrics: Metrics
    actions: ActionRunner | None = None
    memory: MemoryStore | None = None
    handoffs: HandoffStore | None = None
    responses: ResponseStore | None = None


@dataclass
class TurnContext:
    state: SessionState
    policy: CompiledPolicy
    inbound: InboundMessage
    services: Services
    turn_id: str = dc_field(default_factory=lambda: new_id("turn"))
    #: message text after shorthand expansion (original kept in inbound.text)
    text: str = ""
    memory: list[MemoryRecord] = dc_field(default_factory=list)
    system_prompt: str = ""
    verdict: AnswerVerdict | None = None
    route: str | None = None
    reask: bool = False
    planner_detour: bool = False
    node_result: NodeResult | None = None
    guard: GuardContext = dc_field(default_factory=GuardContext)
    outbound: OutboundMessage | None = None
    rendered: list[ChannelMessage] = dc_field(default_factory=list)
    trace: list[StageTrace] = dc_field(default_factory=list)
    short_circuit: bool = False
    input_flagged: bool = False
    #: the last yes/no reading could not reach the model (the caller asks again without counting)
    affirm_failed: bool = False
    errors: list[str] = dc_field(default_factory=list)
    llm_calls: int = 0
    tool_calls: int = 0
    exit_reason: str | None = None
    #: validated FormFill answers this turn (field → value) for the memory writer
    validated_answers: dict[str, Any] = dc_field(default_factory=dict)
    #: entities found by the normalization pre-processor
    entities: dict[str, list[str]] = dc_field(default_factory=dict)
    #: why this turn passes the conversation to a person (a policy `handoff.when` trigger, or follow_up after one)
    handoff_reason: str | None = None
    #: the candidate-only action this turn proves (channel action, or an explicit "submit" reply to the final review)
    candidate_action: str | None = None
    #: decisions a stage records itself (guardrail blocks, handoffs); the runtime adds the rest and stores them
    audit: list[AuditEvent] = dc_field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.text:
            self.text = self.inbound.text
        if self.inbound.action:
            self.candidate_action = self.inbound.action

    def transcript(self, turns: int = 6) -> str:
        """The last few turns and this message, as plain text for a prompt. Flagged turns are left out."""
        recent = [t for t in self.state.turns if not t.flagged][-turns:]
        lines = [f"{'User' if t.role == 'user' else 'Assistant'}: {t.text}" for t in recent]
        if not self.input_flagged:
            lines.append(f"User: {self.text}")
        return "\n".join(lines)

    def decide(self, kind: AuditKind, decision: str, *, reason: str = "", ref: str | None = None, ok: bool | None = None) -> None:
        """Record a decision for the audit log."""
        q = self.state.open_question
        self.audit.append(
            AuditEvent(
                tenant=self.policy.tenant,
                agent=self.policy.agent,
                policy_hash=self.policy.hash,
                session_id=self.state.id,
                turn_id=self.turn_id,
                kind=kind,
                decision=decision,
                reason=reason,
                step=q.step if q else None,
                field=q.field if q else None,
                ok=ok,
                ref=ref,
            )
        )

    def reply(self, text: str, *, goal: str = "", items: list[dict[str, Any]] | None = None) -> None:
        self.node_result = NodeResult(text=text, goal=goal, items=items or [])

    def short_circuit_with(self, text: str, note: str = "") -> None:
        """Reply with a fixed message (already in the session language) and skip to Respond; it is never rephrased."""
        self.reply(text)
        assert self.node_result is not None
        self.node_result.fixed = True
        self.short_circuit = True
        if note:
            self.errors.append(note)

    @property
    def language(self) -> str:
        return self.state.language

    @property
    def memory_values(self) -> dict[str, Any]:
        return {m.item: m.value for m in self.memory}

    @property
    def sensitive_values(self) -> set[str]:
        return {str(m.value) for m in self.memory if m.sensitive}

"""Session State and User Memory records."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> datetime:
    return datetime.now(UTC)


StepStatus = Literal["pending", "in_progress", "complete", "reopened"]


class Turn(BaseModel):
    id: str = Field(default_factory=lambda: new_id("turn"))
    role: Literal["user", "agent"]
    text: str
    original_text: str | None = None
    created_at: datetime = Field(default_factory=now)
    language: str = "en"
    step: str | None = None
    field: str | None = None
    flagged: bool = False


class StepState(BaseModel):
    status: StepStatus = "pending"
    reopen_count: int = 0


class OpenQuestion(BaseModel):
    step: str
    field: str
    ask: str
    expect: str
    #: the answer is free text (a survey's open question): a reply in two parts is one answer, not two values
    free_text: bool = False


class SessionState(BaseModel):
    """One store, one atomic write per turn with an optimistic version check."""

    id: str = Field(default_factory=lambda: new_id("ses"))
    user_id: str
    agent: str
    tenant: str = "default"
    policy_hash: str
    channel: str
    version: int = 0
    created_at: datetime = Field(default_factory=now)
    last_turn_at: datetime = Field(default_factory=now)
    language: str = "en"
    language_evidence: list[str] = Field(default_factory=list)
    pending_language: str | None = None
    turns: list[Turn] = Field(default_factory=list)
    steps: dict[str, StepState] = Field(default_factory=dict)
    fields: dict[str, Any] = Field(default_factory=dict)
    #: answers given before they were asked (field → value), confirmed when the step reaches them
    prefilled: dict[str, Any] = Field(default_factory=dict)
    outputs: dict[str, Any] = Field(default_factory=dict)
    reasks: dict[str, int] = Field(default_factory=dict)
    #: enquiries about the current step, per step (capped by defaults.on_step_enquiries)
    on_step_enquiries: dict[str, int] = Field(default_factory=dict)
    #: failed tool calls per tool name; the runtime exits the step at defaults.max_tool_failures
    tool_failures: dict[str, int] = Field(default_factory=dict)
    digressions: int = 0
    nudges: int = 0
    open_question: OpenQuestion | None = None
    consent: bool | None = None
    completed: bool = False
    exited: bool = False
    exit_reason: str | None = None
    resume_token: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    def step(self, name: str) -> StepState:
        return self.steps.setdefault(name, StepState())

    @property
    def finished(self) -> bool:
        return self.completed or self.exited

    @property
    def retained_short(self) -> bool:
        """Finished and nothing will bring it back: kept only briefly. A handed-off conversation waits for the person's
        report back (and may resume), so it is kept as long as an open one."""
        return self.finished and self.exit_reason != "handed_off"

    def recent_turns(self, n: int = 6) -> list[Turn]:
        """The last `n` turns since the conversation last came back from a person: what was said before was dealt with
        there (an earlier "let me talk to a human" must not hand off again)."""
        since = self.data.get("back_at")
        turns = [t for t in self.turns if t.created_at.isoformat() > since] if since else self.turns
        return turns[-n:]


class MemoryRecord(BaseModel):
    """One record per user per agent per item."""

    user_id: str
    agent: str
    item: str
    tenant: str = "default"
    value: Any
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    source_turn_id: str | None = None
    half_life_s: float
    sensitive: bool = False

    def weight(self, at: datetime | None = None) -> float:
        age = ((at or now()) - self.updated_at).total_seconds()
        return float(0.5 ** (age / self.half_life_s)) if self.half_life_s > 0 else 1.0


class PolicyVersion(BaseModel):
    """One saved build of an agent's policy: the YAML as written and the hash it compiled to. Immutable."""

    tenant: str
    agent: str
    #: 1, 2, ... per tenant and agent
    version: int
    hash: str
    yaml: str
    note: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class AgentSummary(BaseModel):
    agent: str
    #: the version new sessions start on; None until one is activated
    active_version: int | None
    latest_version: int
    updated_at: datetime


class PolicyTemplate(BaseModel):
    """A bot type: a starting policy for new agents. Built-ins come from examples/; tenants save their own."""

    tenant: str
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    yaml: str
    builtin: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


#: what an audit record is about
AuditKind = Literal["turn", "answer", "count", "step", "exit", "handoff", "guardrail", "tool", "llm", "rule"]


def subject_of(tenant: str, user_id: str) -> str:
    """The audit log's pseudonymous id for a user: stable, not reversible."""
    return hashlib.sha256(f"{tenant}\x00{user_id}".encode()).hexdigest()[:32]


class AuditEvent(BaseModel):
    """One decision the platform made (or a person made on a handoff). Append-only. `text` is a short snippet with PII
    and the session's sensitive values removed, and empty for flagged turns."""

    id: str = Field(default_factory=lambda: new_id("aud"))
    at: datetime = Field(default_factory=now)
    tenant: str
    agent: str
    policy_hash: str = ""
    session_id: str | None = None
    turn_id: str | None = None
    kind: AuditKind
    #: what was decided: a route, "accepted", "rejected", "blocked", "opened", "claimed", a tool name's outcome ...
    decision: str
    reason: str = ""
    step: str | None = None
    field: str | None = None
    ok: bool | None = None
    #: what the decision points at: a ticket id, a tool name, a guardrail name
    ref: str | None = None
    #: "runtime", or the person (or API key) who acted on a handoff
    actor: str = "runtime"
    text: str = ""
    #: turn records only: wall time of the turn, model and tool calls, errors (the observability trace, per tenant)
    ms: float | None = None
    llm_calls: int | None = None
    tool_calls: int | None = None
    errors: int | None = None
    #: llm records only (ref = model, reason = purpose): tokens and cost of one model call
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    cost_usd: float | None = None
    #: a one-way hash of the tenant and user (`subject_of`), so a user's words can be cleared on deletion
    subject: str | None = None
    #: turn records: the agent's reply, redacted like `text` (the whole conversation is readable for insights)
    reply: str = ""
    #: llm records only, added later: input tokens a Headroom proxy removed before the call
    saved_tokens: int | None = None


HandoffStatus = Literal["open", "claimed", "resolved"]


class HandoffLine(BaseModel):
    role: Literal["user", "agent"]
    text: str
    at: datetime = Field(default_factory=now)


class SurveyResponse(BaseModel):
    """One survey: the answers as kept (open answers verbatim, closed ones as values) and how each was captured. Kept
    after every answer, so someone who stops part way still counts for what they answered (`complete` False until the
    end). The source for findings; deleted with the user's data."""

    id: str = Field(default_factory=lambda: new_id("rsp"))
    tenant: str
    agent: str
    policy_hash: str
    session_id: str
    user_id: str
    answers: dict[str, Any]
    capture: dict[str, str] = Field(default_factory=dict)
    #: every question was reached; False while it is in progress or after they stopped
    complete: bool = True
    #: what to split results by: campaign, channel, language, and labels from the invite (product, first or repeat buyer)
    segment: dict[str, str] = Field(default_factory=dict)
    #: when it was last updated (finished, or the latest answer)
    completed_at: datetime = Field(default_factory=now)


class Embed(BaseModel):
    """An agent published to the web: a website widget and an installable app. The id is public (it sits in page
    source); it only starts and continues conversations with this one agent, from the allowed sites, within limits."""

    id: str = Field(default_factory=lambda: "pk_" + secrets.token_urlsafe(18))
    tenant: str
    agent: str
    name: str = Field(min_length=1, max_length=80)
    #: sites that may show the widget, as origins: https://www.example.com
    allowed_origins: list[str] = Field(default_factory=list)
    title: str = Field(default="", max_length=60)
    greeting: str = Field(default="", max_length=300)
    #: new conversations per day across all visitors
    daily_limit: int = Field(default=500, ge=1, le=100_000)
    enabled: bool = True
    created_at: datetime = Field(default_factory=now)
    #: WhatsApp Cloud API connection: the business number's id, and the access token and app secret encrypted
    #: (yachat/channels/whatsapp_cloud.py); never returned by the API
    wa_phone_number_id: str = ""
    wa_token: str = ""
    wa_app_secret: str = ""
    wa_verify_token: str = ""

    def view(self) -> dict[str, Any]:
        """What the API shows: never the WhatsApp secrets."""
        return self.model_dump(mode="json", exclude={"wa_token", "wa_app_secret"}) | {"whatsapp_connected": bool(self.wa_token)}


class Handoff(BaseModel):
    """A conversation passed to a person: the platform's record, listed in the inbox. A webhook may also notify a
    helpdesk; its ticket id is kept as `external_id`."""

    id: str = Field(default_factory=lambda: new_id("hof"))
    tenant: str
    agent: str
    session_id: str
    user_id: str
    channel: str
    policy_hash: str
    reason: str
    status: HandoffStatus = "open"
    assignee: str | None = None
    note: str = ""
    fields: dict[str, str] = Field(default_factory=dict)
    transcript: list[HandoffLine] = Field(default_factory=list)
    external_id: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    #: sha256 of the callback token handed to the receiver; never shown (see `public`)
    callback_hash: str = ""

    def public(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"callback_hash"})


class ReviewTurn(BaseModel):
    """One exchange of the conversation under review, with the policy rules that matched on it."""

    model_config = ConfigDict(extra="forbid")

    user: str
    agent: str
    rules: list[str] = Field(default_factory=list)


class ReplyRating(BaseModel):
    """A person's verdict on one reply: does it pass `check` (`in_scope` or `rule_followed:<rule>`)."""

    model_config = ConfigDict(extra="forbid")

    #: the reply's turn, from 1
    turn: int = Field(ge=1)
    check: str = Field(pattern=r"^(in_scope|rule_followed:[A-Za-z0-9_.-]+)$")
    ok: StrictBool
    note: str = Field(default="", max_length=500)


class Review(BaseModel):
    """A person's rating of one conversation from the test chat or a simulation: per-reply verdicts that become
    judge-calibration labels and replay cases, and an overall score."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("rev"))
    tenant: str
    agent: str
    policy_hash: str = ""
    source: Literal["chat", "simulate"]
    #: the session id or the simulated user's name
    conversation: str = Field(min_length=1, max_length=120)
    turns: list[ReviewTurn] = Field(min_length=1, max_length=50)
    replies: list[ReplyRating] = Field(default_factory=list, max_length=200)
    #: overall, 1 (bad) to 5 (great)
    score: int | None = Field(default=None, ge=1, le=5)
    note: str = Field(default="", max_length=2000)
    rater: str = Field(default="", max_length=80)
    created_at: datetime = Field(default_factory=now)

    @model_validator(mode="after")
    def _replies_name_turns(self) -> Review:
        for i, x in enumerate(self.replies):
            if x.turn > len(self.turns):
                raise ValueError(f"replies[{i}].turn: {x.turn}, but the conversation has {len(self.turns)} turns")
        return self

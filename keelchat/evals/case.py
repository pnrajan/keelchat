"""One test conversation and what must hold when it ends: plain data, shared by eval sets (`replay.yaml`) and the tests
stored with an agent (the console's Test tab)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

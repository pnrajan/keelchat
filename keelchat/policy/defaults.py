"""Code defaults (section 8). The compiled policy records every resolved value."""

from __future__ import annotations

from datetime import timedelta

from pydantic import BaseModel, ConfigDict, Field

from keelchat.policy.durations import Duration


class Defaults(BaseModel):
    model_config = ConfigDict(frozen=True)

    reasks: int = 3
    digressions: int = 3
    nudges: int = 1
    on_invalid: str = "clarify"
    memory_half_life: Duration = timedelta(days=180)
    stale_memory_threshold: float = 0.25
    planner_step_budget: int = 6
    token_budget: int = 20_000
    latency_target_s: float = 4.0
    language_switch: str = "explicit ask or two confident turns, then confirm"
    language_confident_turns: int = 2
    input_guardrails: list[str] = Field(default_factory=lambda: ["prompt_injection", "sql_injection"])
    output_guardrails: list[str] = Field(default_factory=lambda: ["pii", "profanity"])
    exit_message: str = "Come back anytime to finish."
    #: rewrite each English draft reply with the model so it reads naturally (other languages are always rewritten,
    #: to translate them); off: English replies are the steps' drafts as written
    natural_replies: bool = True
    #: check replies that no flow step owns (planner detours, re-asks, nudges) before they are sent
    verify_replies: bool = True
    sensitive_memory: list[str] = Field(default_factory=lambda: ["gender"])
    max_reopens: int = 2
    #: enquiries about the current step (e.g. comparing the jobs just offered) before the agent asks to pick
    on_step_enquiries: int = 3
    #: always-on guardrails; a policy cannot remove these
    base_input_guardrails: list[str] = Field(default_factory=lambda: ["prompt_injection"])
    base_output_guardrails: list[str] = Field(default_factory=lambda: ["pii"])
    #: unfinished sessions resume within this window; completed sessions close immediately
    session_ttl_days: int = 7
    #: wall-clock cap for the Intent Detector call; a timeout fails open (treated as new_answer)
    intent_timeout_s: float = 45.0
    #: failed calls of one tool in a session before the flow gives up on it (exit_reason tool_unavailable)
    max_tool_failures: int = 3
    #: wall-clock cap for one planner detour (several model and tool calls)
    planner_timeout_s: float = 120.0
    #: attempts a node may make to fix its own model output (SQL, code, an interpretation) from the error it got,
    #: per stage, for nodes whose decorator declares no repairs
    repairs: int = 2
    #: the policy's `limits.repairs`: a cap on every node's stages, also for nodes run outside the flow (enquiries)
    repair_limit: int | None = None


DEFAULTS = Defaults()

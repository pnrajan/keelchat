"""Step graph over Session State: readiness via `requires`, completion via outputs, reopening."""

from __future__ import annotations

import secrets

from yachat.logging import get_logger
from yachat.policy.compiler import CompiledPolicy, CompiledStep
from yachat.state.models import OpenQuestion, SessionState, StepState

log = get_logger("flow")


class StepGraph:
    def __init__(self, policy: CompiledPolicy):
        self.policy = policy
        self.order = [s.name for s in policy.steps]

    def init(self, state: SessionState) -> None:
        for name in self.order:
            state.steps.setdefault(name, StepState())

    def is_complete(self, state: SessionState, name: str) -> bool:
        return state.step(name).status == "complete"

    def ready(self, state: SessionState, step: CompiledStep) -> bool:
        return all(self.is_complete(state, r) for r in step.requires)

    def next_step(self, state: SessionState) -> str | None:
        """A step already in progress or reopened wins; else the first incomplete ready step in order."""
        for s in self.policy.steps:
            if state.step(s.name).status in ("in_progress", "reopened") and self.ready(state, s):
                return s.name
        for s in self.policy.steps:
            if not self.is_complete(state, s.name) and self.ready(state, s):
                return s.name
        return None

    def all_complete(self, state: SessionState) -> bool:
        return all(self.is_complete(state, n) for n in self.order)

    def start(self, state: SessionState, name: str) -> None:
        st = state.step(name)
        if st.status in ("pending", "reopened"):
            st.status = "in_progress"

    def outputs_valid(self, state: SessionState, step: CompiledStep) -> bool:
        return all(state.outputs.get(o) is not None for o in step.produces)

    def complete(self, state: SessionState, name: str) -> None:
        state.step(name).status = "complete"
        if state.open_question and state.open_question.step == name:
            state.open_question = None
        if self.all_complete(state):
            state.completed = True

    def reopen(self, state: SessionState, name: str) -> bool:
        st = state.step(name)
        if st.reopen_count >= self.policy.defaults.max_reopens:
            log.warning("reopen cap hit", step=name, cap=self.policy.defaults.max_reopens)
            return False
        st.status = "reopened"
        st.reopen_count += 1
        state.completed = False
        log.info("step reopened", step=name, count=st.reopen_count)
        return True

    def clear_output(self, state: SessionState, output: str) -> str | None:
        """Clear an output; reopen the step that produced it. Dependents keep their answers."""
        producer = self.policy.producer_of(output)
        state.outputs.pop(output, None)
        if producer is None or not self.is_complete(state, producer.name):
            return None
        return producer.name if self.reopen(state, producer.name) else None

    def set_open(self, state: SessionState, step: str, field: str, ask: str, expect: str, free_text: bool = False) -> None:
        state.open_question = OpenQuestion(step=step, field=field, ask=ask, expect=expect, free_text=free_text)

    def exit(self, state: SessionState, reason: str) -> str:
        state.exited = True
        state.exit_reason = reason
        state.resume_token = secrets.token_urlsafe(16)
        state.open_question = None
        return state.resume_token

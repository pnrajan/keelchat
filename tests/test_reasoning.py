from __future__ import annotations

from dataclasses import dataclass, field

from tests.helpers import RecordingLLM
from yachat.reasoning import Check, CheckResult, Issue, Plan, Task, normalize_checks, verified
from yachat.runtime.llm import ModelConfig

MODELS = ModelConfig.from_env()


@dataclass
class Sum(Task[str]):
    """A calculation whose first answer is wrong; the right one comes only once a reviewer's problems are fed back."""

    answers: list[str]
    given: list[CheckResult] = field(default_factory=list)
    seen: list[tuple[Plan, list[Check], list[Issue]]] = field(default_factory=list)

    def brief(self) -> str:
        return "Task: compute 17 * 24 + 9 and answer with the number only."

    async def produce(self, plan: Plan, checks: list[Check], feedback: list[Issue]) -> str:
        self.seen.append((plan, checks, feedback))
        return self.answers[min(len(self.seen), len(self.answers)) - 1]

    def evidence(self, out: str) -> str:
        return "The expression: 17 * 24 + 9"

    def results(self, out: str) -> list[CheckResult]:
        return self.given


async def test_plan_and_checks_come_before_the_work_and_a_failed_verification_loops():
    llm = RecordingLLM()
    task = Sum(answers=["399", "417"])
    out, v = await verified(llm, MODELS, task, rounds=2)
    assert out == "417" and v.passed and v.rounds == 2 and v.llm_calls == len(llm.calls) == 3
    assert [c.purpose for c in llm.calls][:2] == ["reasoning_prepare", "reasoning_verify"]  # planned before any work
    plan, checks, first_feedback = task.seen[0]
    assert plan.method and plan.rationale and checks and not first_feedback
    _, _, second_feedback = task.seen[1]
    assert second_feedback and all(i.stage == "answer" for i in second_feedback)  # the wrong answer was sent back
    assert {r.id for r in v.results} == {c.id for c in v.checks} and all(r.passed for r in v.results)


async def test_an_answer_that_never_passes_stops_at_the_budget_unverified():
    task = Sum(answers=["399"])
    out, v = await verified(RecordingLLM(), MODELS, task, rounds=1)
    assert out == "399" and not v.passed and v.rounds == 2 and len(task.seen) == 2 and v.issues


async def test_checks_run_in_code_decide_even_when_the_verifier_is_satisfied():
    failed = CheckResult(id="c_code", passed=False, detail="AssertionError: n == len(df)")
    task = Sum(answers=["417"], given=[failed])
    llm = RecordingLLM()
    _, v = await verified(llm, MODELS, task, rounds=0)
    assert not v.passed and v.rounds == 1 and failed in v.results and any("c_code" in i.problem for i in v.issues)
    assert not llm.calls_for("reasoning_verify") and v.llm_calls == 1  # a failed runnable check needs no verdict


def test_checks_that_cannot_fail_are_not_run_as_code():
    raw = [
        {"id": "c1", "claim": "p-value is a probability", "code": "assert 0 <= answer['p_value'] <= 1"},
        {"id": "c2", "claim": "n matches the rows", "code": "print(answer)"},  # no assert: it cannot fail
        {"id": "c1", "claim": "duplicate id", "code": ""},
        {"id": "", "claim": "", "code": ""},
    ]
    checks = normalize_checks(raw, code=True)
    assert [(c.id, bool(c.code)) for c in checks] == [("c1", True), ("c2", False), ("c3", False)]
    assert all(not c.code for c in normalize_checks(raw, code=False))

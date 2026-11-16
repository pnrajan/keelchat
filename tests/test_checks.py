"""Code checks: pure predicates over a draft and its evidence, run before any model verifier, in the planner and the
Rewriter. The checks are table-tested; the hooks are scripted (deterministic control flow on the real runtime)."""

from __future__ import annotations

from typing import Any

from keelchat.capabilities.checks import no_internal_names, no_reasoning_leak, numbers_grounded, question_kept
from keelchat.registry.core import REGISTRY, Evidence, Finding
from keelchat.runtime.decide.checks import run_checks
from keelchat.testing.fakes import FakeTools
from tests.helpers import ScriptedLLM, make_runtime, say
from tests.test_planner_loop import ANSWER, JOBS, POLICY, run, tool, verdict


def test_the_built_in_checks_reject_what_they_name_and_pass_plain_replies():
    ev = Evidence(
        sources=["job_search → [{'title': 'Backend Engineer', 'pay': '$150k', 'zip': '78701'}]", "what does it pay?"],
        asked="",
        names={"job_search", "form_fill"},
    )
    leak = no_reasoning_leak("The user wants to know the pay. Looking at the tool output, it is $150k.", ev)
    assert leak is not None and leak.issue == "incomplete" and "reasoning" in leak.explanation
    assert no_reasoning_leak("The Backend Engineer role pays $150k.", ev) is None
    assert no_internal_names("I ran job_search for you.", ev) is not None and no_internal_names("Per the plain version, $150k.", ev) is not None
    assert no_internal_names("I searched the jobs for you: $150k.", ev) is None
    loose = numbers_grounded("It pays $1800k a year in 78701.", ev)
    assert loose is not None and loose.issue == "calculation_error" and loose.unsupported == ("$1800k",)
    assert numbers_grounded("It pays $1,500k.", Evidence(sources=["pay: $1,500k"])) is None  # the same tokens on both sides
    assert numbers_grounded("Backend Engineer, **$150k**, zip 78701. 1. first 2. second", ev) is None  # markup, list markers
    assert question_kept("Thanks.", Evidence(asked="What is your full name?")) is not None and question_kept("Thanks. Your name?", Evidence(asked="x")) is None
    assert question_kept("Thanks.", Evidence()) is None


def test_run_checks_returns_the_first_rejection_and_survives_a_broken_check():
    from keelchat.registry.core import CheckSpec

    def boom(draft: str, ev: Evidence) -> Finding | None:
        raise ValueError("bug")

    REGISTRY.add_check(CheckSpec(name="aaa_broken", fn=boom))  # sorts first; must not take the turn down
    try:
        hit = run_checks("The user wants the pay; it is $999k.", Evidence(sources=["$150k"]))
        assert hit is not None and hit[0] == "no_reasoning_leak"
        assert run_checks("It pays $150k.", Evidence(sources=["$150k"])) is None
    finally:
        REGISTRY._checks.pop("aaa_broken", None)
    assert {"no_internal_names", "no_reasoning_leak", "numbers_grounded", "question_kept"} <= {c.name for c in REGISTRY.checks()}


async def test_a_check_rejects_a_draft_before_the_model_verifier_is_asked() -> None:
    """The first draft is a model thinking out loud; code sends it back with typed feedback and the verifier is not
    called for it. The second draft is a reply, verified once, delivered."""
    r, llm = await run(
        {
            "plan": [tool(role="backend", location="Austin"), ANSWER, ANSWER],
            "planner_answer": [
                "The user wants to know the pay. Looking at the search, Backend Engineer pays $150k.",
                "Backend Engineer in Austin pays $150k.",
            ],
            "verify_grounded": [verdict()],
        }
    )
    plans = llm.calls_for("plan")
    assert plans[2].metadata["feedback"]["issue"] == "incomplete" and "check no_reasoning_leak" in plans[2].metadata["feedback"]["explanation"]
    assert len(llm.calls_for("verify_grounded")) == 1 and "$150k" in r.outbound.text and "the user wants" not in r.outbound.text.lower()


async def test_a_check_sends_a_leaky_rewrite_back_for_repair() -> None:
    """The Rewriter's first rendering deliberates; the check rejects it and the second rendering is sent."""
    scripts: dict[str, list[Any]] = {
        "plan": [tool(role="backend", location="Austin"), ANSWER],
        "planner_answer": ["Backend Engineer in Austin pays $150k."],
        "verify_grounded": [verdict()],
        "render": ["The user wants the pay. I should say it is $150k.", "The Backend Engineer role in Austin pays $150k."],
        "reply_verify": [{"ok": True, "problems": []}],
    }
    r, llm = await run(scripts)
    assert len(llm.calls_for("render")) == 2 and "$150k" in r.outbound.text and "i should" not in r.outbound.text.lower(), r.outbound.text


def test_the_check_kind_is_in_the_registry_manifest_and_the_contract_suite():
    from keelchat.registry.contract import check_check

    names = {e.name for e in REGISTRY.manifest().entries if e.kind == "check"}
    assert {"no_reasoning_leak", "no_internal_names", "numbers_grounded", "question_kept"} <= names
    for n in names:
        check_check(REGISTRY.check(n))


async def test_a_figure_from_an_earlier_turn_is_grounded() -> None:
    """The planner's sources are what its verifier reads: this turn's tool results and the conversation. An answer that
    rests on what an earlier turn listed ($150k) is not rejected as a figure from nowhere."""
    llm = ScriptedLLM(
        {
            "plan": [tool(role="backend", location="Austin"), ANSWER, ANSWER],
            "planner_answer": ["Backend Engineer in Austin, $150k.", "The Backend Engineer pays $150k."],
            "verify_grounded": [verdict(), verdict()],
        }
    )
    rt, _ = await make_runtime(POLICY, llm=llm, tools=FakeTools(jobs=JOBS))
    s = await rt.start_session("jobs-helper", "u1")
    await say(rt, s.id, "What backend jobs are open in Austin?")
    r = await say(rt, s.id, "Which pays more?")  # no lookup this turn: the figure is in the conversation
    assert len(llm.calls_for("verify_grounded")) == 2 and r.tool_calls == 0 and "$150k" in r.outbound.text, r.outbound.text
    assert all(p.metadata["feedback"] is None for p in llm.calls_for("plan"))  # no check sent the planner back

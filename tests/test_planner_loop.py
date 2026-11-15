"""The planner–verifier loop: the verifier reports a typed gap, the planner decides what to do about it, code bounds
the loop and never delivers a rejected draft as checked. Plan, draft and verify are scripted (deterministic control
flow); the rest of the turn runs on the real model."""

from __future__ import annotations

from typing import Any

from keelchat.runtime.decide.planner import FALLBACK, VerifierFeedback, gap_for_user, read_feedback
from keelchat.runtime.llm import LLMError
from keelchat.testing.fakes import FakeTools
from tests.helpers import ScriptedLLM, make_runtime, say

POLICY = """agent: jobs-helper
charter: "Answer questions about open jobs at our company. Out of scope: anything else."
tools: [job_search]
mode: planner
"""
JOBS = [{"id": "j1", "title": "Backend Engineer", "location": "Austin", "pay": "$150k"}]


def tool(**args: Any) -> dict[str, Any]:
    return {"action": "tool", "tool": "job_search", "args": args, "question": None, "rationale": "look"}


ANSWER = {"action": "answer", "tool": None, "args": {}, "question": None, "rationale": "enough"}


def ask(q: str) -> dict[str, Any]:
    return {"action": "ask", "tool": None, "args": {}, "question": q, "rationale": "need it"}


def verdict(
    issue: str = "none", unmet: str = "", unsupported: list[str] | None = None, answers_ask: bool | None = None, repairable: bool = True
) -> dict[str, Any]:
    return {
        "unsupported": unsupported or [],
        "answers_ask": (issue == "none") if answers_ask is None else answers_ask,
        "issue": issue,
        "unmet": unmet,
        "evidence": ["job_search"],
        "repairable": repairable,
        "reason": f"{issue}: {unmet}" if unmet else issue,
    }


async def run(scripts: dict[str, list[Any]], text: str = "What backend jobs are open in Austin and what do they pay?") -> tuple[Any, ScriptedLLM]:
    llm = ScriptedLLM(scripts)
    rt, _ = await make_runtime(POLICY, llm=llm, tools=FakeTools(jobs=JOBS))
    s = await rt.start_session("jobs-helper", "u1")
    return await say(rt, s.id, text), llm


def test_feedback_is_typed_and_a_bad_verdict_is_no_verdict() -> None:
    fb = read_feedback(verdict("incomplete", "the pay"))
    assert fb is not None and fb.issue == "incomplete" and fb.unmet == "the pay" and not fb.passed and "recalculate" not in fb.as_prompt()
    assert read_feedback(verdict()) is not None and read_feedback(verdict()).passed  # type: ignore[union-attr]
    assert read_feedback({"reason": "ok"}) is None and read_feedback({"issue": "made_up", "unsupported": []}) is None
    assert not VerifierFeedback(issue="none", unsupported=["$999k"], answers_ask=True).passed  # one unsupported figure fails it


async def test_an_incomplete_draft_gets_feedback_then_a_complete_answer() -> None:
    r, llm = await run(
        {
            "plan": [tool(role="backend", location="Austin"), ANSWER, ANSWER],
            "planner_answer": ["There is a Backend Engineer role in Austin.", "There is a Backend Engineer role in Austin paying $150k."],
            "verify_grounded": [verdict("incomplete", "the pay"), verdict()],
        }
    )
    plans = llm.calls_for("plan")
    assert len(plans) == 3 and plans[1].metadata["feedback"] is None and plans[2].metadata["feedback"]["issue"] == "incomplete"
    assert "the pay" in plans[2].system and "verifier_feedback" in plans[2].system  # the planner saw what was missing
    assert "$150k" in r.outbound.text and r.tool_calls == 1 and not r.errors, r.outbound.text
    assert not r.state.completed  # a checked answer is not a completed goal


async def test_missing_evidence_sends_the_planner_back_to_a_tool() -> None:
    r, llm = await run(
        {
            "plan": [ANSWER, tool(role="backend", location="Austin"), ANSWER],
            "planner_answer": ["I believe there is a backend role.", "Backend Engineer in Austin, $150k."],
            "verify_grounded": [verdict("missing_evidence", "no lookup was made", answers_ask=False), verdict()],
        }
    )
    plans = llm.calls_for("plan")
    assert plans[1].metadata["feedback"]["issue"] == "missing_evidence" and r.tool_calls == 1
    assert len(llm.calls_for("verify_grounded")) == 2 and "$150k" in r.outbound.text


async def test_a_calculation_error_asks_for_a_recalculation() -> None:
    r, llm = await run(
        {
            "plan": [tool(role="backend", location="Austin"), ANSWER, ANSWER],
            "planner_answer": ["That is $1,800k a year.", "That is $150k a year."],
            "verify_grounded": [verdict("calculation_error", "annual pay", unsupported=["$1,800k"]), verdict()],
        }
    )
    drafts = llm.calls_for("planner_answer")
    assert len(drafts) == 2 and "recalculate" in drafts[1].system and "$1,800k" not in r.outbound.text and "$150k" in r.outbound.text


async def test_missing_user_information_becomes_one_question() -> None:
    r, llm = await run(
        {
            "plan": [ANSWER, ask("Which city are you looking in?")],
            "planner_answer": ["There are roles in several cities."],
            "verify_grounded": [verdict("missing_user_information", "the city", answers_ask=False)],
        },
        text="Any backend jobs?",
    )
    assert len(llm.calls_for("plan")) == 2 and len(llm.calls_for("verify_grounded")) == 1
    assert "city" in r.outbound.text.lower() and r.tool_calls == 0, r.outbound.text


async def test_the_same_failure_twice_stops_the_loop_with_a_supported_partial_answer() -> None:
    r, llm = await run(
        {
            "plan": [tool(role="backend", location="Austin")] + [ANSWER] * 6,
            "planner_answer": ["Backend Engineer in Austin."] * 6,
            "verify_grounded": [verdict("incomplete", "the pay")] * 6,
        }
    )
    assert len(llm.calls_for("verify_grounded")) == 2  # no progress after two identical verdicts: bounded, not the budget
    assert "Backend Engineer" in r.outbound.text and "pay" in r.outbound.text.lower(), r.outbound.text  # what held, and what did not


async def test_an_unrepairable_or_policy_breaking_draft_falls_back_safely() -> None:
    r, llm = await run(
        {
            "plan": [tool(role="backend", location="Austin"), ANSWER],
            "planner_answer": ["Legally you must sue them."],
            "verify_grounded": [verdict("policy_violation", "out of scope", answers_ask=False, repairable=False)],
        }
    )
    assert len(llm.calls_for("plan")) == 2 and "sue" not in r.outbound.text.lower()


async def test_a_verifier_outage_or_malformed_verdict_approves_nothing() -> None:
    r, llm = await run(
        {"plan": [tool(role="backend", location="Austin"), ANSWER], "planner_answer": ["Pays $999k."], "verify_grounded": [LLMError("provider down")]}
    )
    assert "$999k" not in r.outbound.text and "planner verifier: LLMError" in r.errors and len(llm.calls_for("plan")) == 2
    malformed = {"plan": [tool(role="backend", location="Austin"), ANSWER], "planner_answer": ["Pays $999k."], "verify_grounded": [{"reason": "fine"}]}
    r2, llm2 = await run(malformed)
    assert "$999k" not in r2.outbound.text and len(llm2.calls_for("plan")) == 2
    assert FALLBACK.split(".")[0].lower()[:12] in r2.outbound.text.lower() or "couldn't" in r2.outbound.text.lower(), r2.outbound.text


async def test_a_question_is_not_a_way_out_of_looking_things_up() -> None:
    """`ask` is accepted only after the verifier said the user holds what is missing; asked for cold, it is ignored and
    the planner is held to the lookups and the draft."""
    r, llm = await run(
        {
            "plan": [ask("Which city?"), tool(role="backend", location="Austin"), ANSWER],
            "planner_answer": ["No idea yet.", "Backend Engineer in Austin, $150k."],
            "verify_grounded": [verdict("missing_evidence", "no lookup", answers_ask=False), verdict()],
        }
    )
    assert "ask" not in llm.calls_for("plan")[0].system.split("Choose action")[1][:120]  # not offered cold
    assert r.tool_calls == 1 and "$150k" in r.outbound.text and "city" not in r.outbound.text.lower()


def test_the_gap_named_to_the_user_is_a_short_phrase_never_the_verifiers_commentary() -> None:
    assert gap_for_user("the pay for each role") == "the pay for each role"
    assert gap_for_user("The pay.") == "the pay"
    long = "The user asked for open backend jobs in Austin and their pay; the draft explicitly states it cannot provide this."
    assert gap_for_user(long) == gap_for_user("") == gap_for_user("what the draft says") == "part of what was asked"


async def test_a_tool_that_is_down_is_not_described_to_the_user() -> None:
    """With no backend configured the tool fails with kind `config`; the user hears neither that word nor "circuit"."""
    from keelchat.runtime.decide.tool_gateway import ToolGateway

    gw = ToolGateway({"job_search"}, {})  # the real tool, no JOB_SEARCH_URL
    res = await gw.call("job_search", {"role": "backend", "location": "Austin"})
    assert not res.ok and res.error_kind == "config"
    prompt = res.as_prompt_data()
    assert "config" not in prompt and "unavailable" in prompt

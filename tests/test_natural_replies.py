from __future__ import annotations

from keelchat.capabilities.nodes.form_fill import says_submit
from keelchat.runtime.respond.response_generator import strip_fences
from keelchat.runtime.respond.rewriter import kept
from keelchat.testing.fakes import FakeTools
from tests.conftest import ROOT
from tests.helpers import RecordingLLM, make_runtime, reach_form, say

SUPPORT = (ROOT / "examples" / "support_assistant.yaml").read_text()
COACH = (ROOT / "examples" / "career_coach.yaml").read_text()


def test_a_rewrite_must_keep_every_fact_and_still_ask():
    draft = "Thanks.\nWhat is your order number?"
    assert kept(draft, "Sorry to hear that. Could you share the order number?", asks=True)  # its own words
    assert not kept(draft, "Sorry to hear that, let's sort it out.", asks=True)  # dropped the question
    facts = "US leads with $30,990, EU trails at 10,450. Order ORD-48213 ships 2026-11-02."
    assert kept(facts, "The US is ahead at $30990 and EU is at 10,450; order ORD-48213 ships on 2026-11-02.", asks=False)
    assert not kept(facts, "The US leads and EU trails; order ORD-48213 ships soon.", asks=False)  # figures dropped
    assert not kept("Thanks.", "", asks=False) and not kept("Thanks.", "x" * 2000, asks=False)


def test_a_rewrite_never_lists_items_the_step_shows_itself():
    draft = "Here is what I found:\nWhich job interests you? Reply with its number, or 'none'."
    assert kept(draft, "Here are some matches. Which one interests you?", asks=True, listed=True)
    invented = "Here is what I found:\n1. Senior Software Engineer - Seattle\n2. DevOps Engineer - Remote\nWhich one interests you?"
    assert not kept(draft, invented, asks=True, listed=True)
    assert not kept(draft, "Matches:\n- DevOps Engineer\nWhich one interests you?", asks=True, listed=True)


def test_internal_fences_never_reach_the_user():
    leaked = 'Let me look that up.\n\n<tool_output tool="skills_search" query="PM">\n[]\n</tool_output>\nHere is a plan.'
    assert strip_fences(leaked) == "Let me look that up.\n\nHere is a plan."
    assert strip_fences('Answer <data trusted="false">rows') == "Answer"  # an unclosed fence is cut to the end
    assert strip_fences('Plan:\n<tool_call name="skills_search" args={}>\n</tool_call_result>\nNext?') == "Plan:\n\nNext?"
    assert strip_fences("Use 3 < 5 and a <b>bold</b> word.") == "Use 3 < 5 and a <b>bold</b> word."


def test_only_an_explicit_instruction_submits():
    for t in ("submit", "yes, submit it", "ok send it", "please submit my application", "go ahead, submit it now", "sí, envíalo"):
        assert says_submit(t), t
    for t in ("can you send me a copy?", "how do I apply?", "don't submit yet", "submit after I fix the address", "no"):
        assert not says_submit(t), t


async def test_replies_respond_to_what_the_user_said_and_keep_the_question():
    llm = RecordingLLM()
    rt, _ = await make_runtime(SUPPORT, llm=llm)
    s = await rt.start_session("support-assistant", "u1")
    r = await say(rt, s.id, "hello, my shoes arrived damaged")
    ask = r.state.open_question.ask
    assert "?" in r.outbound.text and r.outbound.text != f"Hi! {ask}", r.outbound.text  # phrased for this conversation
    render = llm.calls_for("render")[0].last_user_text
    assert "shoes arrived damaged" in render and "Ask for: consent" in render and "This step: Greets" in render
    r = await say(rt, s.id, "yes that's fine")
    assert "order number" in r.outbound.text.lower() and "Got it" not in r.outbound.text


async def test_yes_submit_it_submits(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    r = await say(rt, s.id, "123 Main Street, Austin TX 78701")
    assert r.state.open_question.field == "final_review"
    r = await say(rt, s.id, "yes, submit it")
    assert tools.submitted and r.outbound.exit_reason == "completed" and "Come back anytime" not in r.outbound.text


async def test_a_question_after_the_end_is_answered(policy_yaml):
    tools = FakeTools(passages=[{"title": "Benefits", "text": "Full-time staff get health, dental and vision insurance.", "source": "hr"}])
    rt, _ = await make_runtime(policy_yaml, tools=tools)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    await say(rt, s.id, "123 Main Street, Austin TX 78701")
    await say(rt, s.id, "submit")
    r = await say(rt, s.id, "do you offer dental insurance?")
    assert "dental" in r.outbound.text.lower() and "already completed" not in r.outbound.text, r.outbound.text


async def test_the_planner_remembers_what_the_user_said():
    llm = RecordingLLM()
    rt, _ = await make_runtime(COACH, llm=llm)
    s = await rt.start_session("career-coach", "u1")
    await say(rt, s.id, "I'm a data analyst and want to move into product management in two years")
    r = await say(rt, s.id, "what skills should I build first?")
    plan = llm.calls_for("plan")[-1]
    assert "data analyst" in plan.system and "<tool_output" not in r.outbound.text


def test_each_step_verifies_its_replies_unless_it_passes_them_through():
    from keelchat.policy.compiler import build

    on = build(SUPPORT)
    assert all(s.verify for s in on.steps)  # every node has a reply verifier by default
    off = build(SUPPORT.replace("  - greet_consent\n", "  - greet_consent: {verify: false}\n"))
    assert {s.name: s.verify for s in off.steps} == {"greet_consent": False, "form_fill": True} and off.hash != on.hash


class Inventive(RecordingLLM):
    """The real model, except that the first reply it phrases promises something nobody offered."""

    planted = False

    async def complete(self, req):
        out = await super().complete(req)
        if req.purpose == "render" and not self.planted:
            self.planted = True
            return out.rstrip("?") + "? We'll also email you a free return label right away."
        return out


async def test_the_reply_verifier_catches_an_invented_promise():
    llm = Inventive()
    rt, _ = await make_runtime(SUPPORT, llm=llm)
    s = await rt.start_session("support-assistant", "u1")
    r = await say(rt, s.id, "hello, my shoes arrived damaged")
    assert llm.calls_for("reply_verify") and "return label" not in r.outbound.text, r.outbound.text
    assert len(llm.calls_for("render")) == 2 or r.outbound.text.startswith("Hi! Before we start")  # rewritten once, or the plain version
    off = SUPPORT.replace("  - greet_consent\n", "  - greet_consent: {verify: false}\n")
    llm2 = RecordingLLM()
    rt2, _ = await make_runtime(off, llm=llm2)
    s2 = await rt2.start_session("support-assistant", "u2")
    await say(rt2, s2.id, "hi")
    assert not llm2.calls_for("reply_verify")  # passed through


async def test_an_empty_volunteered_value_is_not_offered_back():
    rt, _ = await make_runtime(SUPPORT)
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hello, my shoes arrived damaged")
    st = await rt.session_state(s.id)
    assert all(str(v).strip() for v in st.prefilled.values()), st.prefilled
    r = await say(rt, s.id, "yes that's fine")
    assert "You mentioned ." not in r.outbound.text and "order number" in r.outbound.text.lower(), r.outbound.text


class Rejecting(RecordingLLM):
    """The real model, except the reply verifier rejects every rewrite, so the plain version is the fallback."""

    async def structured(self, req, schema):
        if req.purpose == "reply_verify":
            self.calls.append(req)
            return {"ok": False, "problems": ["rejected for the test"]}
        return await super().structured(req, schema)


async def test_the_fallback_reply_stays_in_the_session_language(policy_yaml):
    llm = Rejecting()
    rt, _ = await make_runtime(policy_yaml, llm=llm)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hola, busco trabajo en Austin")
    await say(rt, s.id, "sí")
    await say(rt, s.id, "ingeniero en Austin")
    r = await say(rt, s.id, "La segunda")
    assert r.state.language == "es" and llm.calls_for("reply_verify") and r.state.open_question.field == "full_name"
    text = r.outbound.text.lower()
    assert "full name" not in text and "great choice" not in text and "?" in text, r.outbound.text  # translated, not English

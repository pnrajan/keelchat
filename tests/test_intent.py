from __future__ import annotations

from tests.helpers import RecordingLLM, make_runtime, mutate, reach_form, say
from yachat.runtime.decide.intent_detector import ROUTE_REASK
from yachat.runtime.llm import LLMError


async def test_required_address_fails_twice_exits_max_reasks(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    r = await say(rt, s.id, "my address is downtown")
    assert "address" in r.outbound.text.lower() and r.state.reasks["address"] == 1
    r = await say(rt, s.id, "my address is near the park")
    assert r.outbound.exit_reason == "max_reasks" and r.state.exited and "Come back anytime to finish." in r.outbound.text
    assert r.state.fields["full_name"] == "Jane Doe"


async def test_benefits_question_mid_form_knowledge_then_reset(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "do you offer dental benefits?")
    assert "dental" in r.outbound.text and tools.calls["knowledge_search"][0]["query"].startswith("do you offer")
    assert "What is your full name?" in r.outbound.text and r.state.digressions == 1
    r = await say(rt, s.id, "Jane Doe")
    assert r.state.digressions == 0 and r.state.fields["full_name"] == "Jane Doe" and "home address" in r.outbound.text.lower()


async def test_three_digressions_nudge_fourth_exits(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "what about benefits?")
    await say(rt, s.id, "is it remote?")
    r3 = await say(rt, s.id, "how about parking?")
    assert r3.state.nudges == 1 and "back on track" in r3.outbound.text and not r3.state.exited
    r4 = await say(rt, s.id, "and the cafeteria?")
    assert r4.state.exited and r4.outbound.exit_reason == "disengaged"


async def test_multi_answer_fills_both_fields_and_confirms(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "Ana Ruiz, 12 Elm St, Austin TX 78704")
    assert r.state.fields["full_name"] == "Ana Ruiz" and "12 Elm St" in r.state.fields["address"]
    assert "full name: Ana Ruiz" in r.outbound.text and "address: 12 Elm St" in r.outbound.text
    assert "What is your home address?" not in r.outbound.text and r.state.open_question.field == "final_review"


async def test_location_given_before_asked_is_not_asked_again(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    r = await say(rt, s.id, "yes, I live in Austin")
    assert r.state.prefilled == {"location": "Austin"} and "role and location" in r.outbound.text
    r = await say(rt, s.id, "backend engineer")
    assert "You mentioned Austin" in r.outbound.text and r.state.open_question.field == "location__confirm"
    r = await say(rt, s.id, "yes")
    assert tools.calls["job_search"][0] == {"role": "backend engineer", "location": "Austin"} and r.state.open_question.field == "job_pick"


async def test_edit_location_after_moving_on_reopens_job_search(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    r = await say(rt, s.id, "Actually, I meant Dallas")
    assert r.state.fields["location"] == "Dallas" and r.state.fields["full_name"] == "Jane Doe"
    assert r.state.steps["job_search"].status == "in_progress" and r.state.steps["job_search"].reopen_count == 1
    assert tools.calls["job_search"][-1] == {"role": "backend engineer", "location": "Dallas"} and "in Dallas" in r.outbound.text
    r = await say(rt, s.id, "1")
    assert r.state.outputs["selected_job"]["id"] == "j1" and "home address" in r.outbound.text.lower()


async def test_optional_field_skipped_at_limit(policy_yaml):
    def fn(d):
        d["flow"][2]["form_fill"]["fields"][1]["optional"] = True

    rt, _ = await make_runtime(mutate(policy_yaml, fn))
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    await say(rt, s.id, "my address is downtown")
    r = await say(rt, s.id, "my address is near the park")
    assert r.state.fields["address"] is None and r.state.open_question.field == "final_review"


async def test_answer_and_enquiry_in_one_message(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hola, busco trabajo en Austin")
    await say(rt, s.id, "sí")
    await say(rt, s.id, "ingeniero en Austin")
    r = await say(rt, s.id, "La segunda. ¿Tienen seguro médico?")
    assert r.state.outputs["selected_job"]["id"] == "j2" and r.state.digressions == 0
    assert tools.calls["knowledge_search"] and "seguro médico" in tools.calls["knowledge_search"][0]["query"]
    assert any(w in r.outbound.text.lower() for w in ("seguro", "dental", "médic", "health")) and r.state.open_question.field == "full_name"
    assert "nombre" in r.outbound.text.lower()  # the next question, in the session's language


async def test_conflicting_values_ask_one_clarifying_question(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    r = await say(rt, s.id, "Actually, it's 12 Elm St. Actually, it's 99 Oak Ave.")
    # the reply is reworded by the model, so check what it does: asks about the address and saves neither value
    text = r.outbound.text.lower()
    assert "address" in text and "?" in text and "address" not in r.state.fields, r.outbound.text


async def test_edit_after_submit_is_a_followup_not_a_reopen(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    await say(rt, s.id, "123 Main St, Austin TX 78701")
    r = await say(rt, s.id, "submit")
    assert tools.submitted and r.state.completed
    r = await say(rt, s.id, "Actually, I meant Dallas")
    assert r.state.steps["job_search"].status == "complete" and r.state.steps["job_search"].reopen_count == 0
    assert r.state.data["followups"][0]["fields"] == {"location": "Dallas"} and "follow-up" in r.outbound.text
    assert len(tools.calls["job_search"]) == 1


async def test_on_step_enquiry_is_answered_not_counted(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    await say(rt, s.id, "yes")
    await say(rt, s.id, "engineer in austin")
    r = await say(rt, s.id, "is the first one remote?")
    assert r.state.digressions == 0 and tools.calls["knowledge_search"] and "Which job interests you" in r.outbound.text
    r = await say(rt, s.id, "do you offer dental benefits?")
    assert r.state.digressions == 1


async def test_on_step_enquiries_are_capped_per_step(policy_yaml):
    rt, tools = await make_runtime(mutate(policy_yaml, lambda d: d.update(limits={"on_step_enquiries": 1})))
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    await say(rt, s.id, "yes")
    await say(rt, s.id, "backend engineer in austin")
    r1 = await say(rt, s.id, "is the first one remote?")
    r2 = await say(rt, s.id, "is the second one remote?")
    assert r1.state.digressions == 0 and r2.state.digressions == 0  # on-step enquiries are never digressions
    assert r2.state.on_step_enquiries["job_search"] == 2 and r2.route == ROUTE_REASK and "Which job" in r2.outbound.text
    assert len(tools.calls["knowledge_search"]) == 1  # the second one was not answered; the user is asked to pick


async def test_out_of_scope_mid_form_says_so_then_reasks():
    from tests.conftest import ROOT

    rt, _ = await make_runtime((ROOT / "examples" / "support_assistant.yaml").read_text())
    s = await rt.start_session("support-assistant", "u1")
    await say(rt, s.id, "hi")
    await say(rt, s.id, "yes")
    r = await say(rt, s.id, "can you give me legal advice about suing my landlord?")
    # declined and counted, then the question again (the wording is the model's: the support example plans its replies)
    assert r.route == ROUTE_REASK and r.state.digressions == 1 and "order number" in r.outbound.text.lower(), r.outbound.text
    assert not r.state.fields


class DetectorDown(RecordingLLM):
    """The real model, except the intent detector call fails (a provider timeout, a dropped connection)."""

    async def structured(self, req, schema):
        if req.purpose == "intent_detect":
            self.calls.append(req)
            raise LLMError("APIConnectionError: connection dropped")
        return await super().structured(req, schema)


async def test_detector_failure_reasks_without_counting_toward_an_exit(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    rt.llm = rt.memory_writer.llm = DetectorDown()
    for _ in range(4):  # more failures than the address re-ask limit
        r = await say(rt, s.id, "actually I don't want that job")
        assert "didn't catch that" in r.outbound.text.lower() and r.state.open_question.field in ("full_name", "address")
        assert not r.state.exited and not r.state.reasks and r.state.digressions == 0


async def test_a_greeting_mid_conversation_is_never_counted_as_off_topic(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    for text in ("hello", "Hi there!", "good morning"):  # the consent question is open
        r = await say(rt, s.id, text)
        assert r.state.digressions == 0 and r.state.open_question.field == "consent", r.outbound.text
        assert "outside what I can help with" not in r.outbound.text

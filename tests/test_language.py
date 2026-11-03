from __future__ import annotations

from tests.helpers import make_runtime, reach_form, say
from yachat.runtime.understand.language_rules import evidence, explicit_request, scores


async def test_spanish_in_spanish_out_french_unsupported(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    r = await say(rt, s.id, "hola, busco trabajo en Madrid")
    assert r.state.language == "es" and r.outbound.language == "es" and "Do you consent" not in r.outbound.text  # rendered in Spanish
    s2 = await rt.start_session("hiring-agent", "u2")
    r = await say(rt, s2.id, "Bonjour, je cherche un emploi à Paris", user="u2")
    assert r.outbound.language == "en" and r.outbound.text == "Sorry, I can only help in English, Spanish." and r.state.language == "en"


async def test_name_answer_does_not_switch(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "José García")
    assert r.state.language == "en" and r.state.fields["full_name"] == "José García" and not r.state.pending_language


async def test_explicit_request_confirms_then_switches(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    r = await say(rt, s.id, "puedo seguir en español?")
    assert "español" in r.outbound.text and "?" in r.outbound.text and r.state.language == "en" and r.state.pending_language == "es"
    r = await say(rt, s.id, "sí")
    assert r.state.language == "es" and "Perfecto" in r.outbound.text
    r = await say(rt, s.id, "yes")
    assert r.outbound.language == "es" and "role and location" not in r.outbound.text  # rendered in Spanish


async def test_explicit_request_declined_keeps_language(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    await say(rt, s.id, "can we switch to spanish?")
    r = await say(rt, s.id, "no")
    assert r.state.language == "en" and "stay in English" in r.outbound.text and r.state.pending_language is None


async def test_two_confident_turns_trigger_confirmation(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    await say(rt, s.id, "yes")
    r = await say(rt, s.id, "hola quiero trabajo de enfermera")
    assert r.state.language == "en" and r.state.pending_language is None and r.state.language_evidence == ["es"]
    r = await say(rt, s.id, "busco empleo en la ciudad por favor")
    assert r.state.pending_language == "es" and "¿Seguimos en español?" in r.outbound.text


async def test_mixed_message_keeps_language_and_extracts(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "si, I can start monday")
    assert r.state.language == "en" and not r.state.pending_language  # one Spanish word is not a switch


async def test_job_results_in_spanish_session_are_verbatim_and_labeled(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hola, busco trabajo en Madrid")
    await say(rt, s.id, "sí")
    r = await say(rt, s.id, "quiero un puesto de ingeniero en Austin")
    assert r.outbound.language == "es" and "Backend Engineer — Austin, TX 78701" in r.outbound.text and "(tal como se publicó)" in r.outbound.text


async def test_shorthand_and_typos(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hola, busco trabajo en Madrid")
    await say(rt, s.id, "sí")
    r = await say(rt, s.id, "busco trabjo d enfermera")
    assert r.state.turns[-2].original_text == "busco trabjo d enfermera" and "enfermera" in r.state.fields["role"]
    await say(rt, s.id, "sí")  # Madrid was volunteered in the opening message; the step confirms it before searching
    assert "enfermera" in tools.calls["job_search"][0]["role"] and tools.calls["job_search"][0]["location"] == "Madrid"
    s2 = await rt.start_session("hiring-agent", "u2", channel="sms")
    await say(rt, s2.id, "hello", channel="sms", user="u2")
    await say(rt, s2.id, "yes", channel="sms", user="u2")
    r = await say(rt, s2.id, "u hav wrk 2mrw nr me", channel="sms", user="u2")
    assert len(tools.calls["job_search"]) == 2 and "2mrw" not in str(tools.calls["job_search"][-1]) and r.outbound.items  # shorthand expanded


async def test_ok_and_si_never_change_language(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    r = await say(rt, s.id, "si")
    assert r.state.language == "en" and r.state.pending_language is None and r.state.steps["greet_consent"].status == "complete"
    assert evidence("ok", "en", None) is None and evidence("si", "es", None) is None


def test_rules():
    assert scores("hola, busco trabajo en Madrid")["es"] >= 2
    assert explicit_request("puedo seguir en español?") == "es" and explicit_request("switch to english please") == "en"
    assert evidence("José García", "en", "full_name") is None
    assert evidence("si, I can start monday", "en", "role") is None

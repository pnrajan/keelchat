from __future__ import annotations

from tests.conftest import ROOT
from tests.helpers import fill_form, make_runtime, mutate, reach_form, say
from yachat.policy.compiler import build
from yachat.registry.contract import make_context
from yachat.runtime.decide.intent_detector import ROUTE_PLANNER
from yachat.runtime.decide.planner import VERIFY_SCHEMA, grounded, planner_tools, verify_system
from yachat.runtime.llm import LLMRequest, ModelConfig
from yachat.runtime.llm_factory import llm_from_env


async def test_happy_path_every_step_complete(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    r = await say(rt, s.id, "hello")
    assert r.state.open_question.field == "consent" and "?" in r.outbound.text
    r = await say(rt, s.id, "yes")
    assert r.state.open_question.field == "role" and "role" in r.outbound.text.lower()
    r = await say(rt, s.id, "backend engineer in austin")
    first = {k: v.lower() for k, v in tools.calls["job_search"][0].items()}
    assert "engineer" in first["role"] and first["location"] == "austin"
    assert "Backend Engineer" in r.outbound.text and "(as posted)" in r.outbound.text and r.outbound.items[0]["id"] == "j1"
    r = await say(rt, s.id, "1")
    assert "full name" in r.outbound.text.lower() and r.state.outputs["selected_job"]["id"] == "j1"
    r = await say(rt, s.id, "Jane Doe")
    assert "home address" in r.outbound.text.lower()
    r = await fill_form_tail(rt, s.id)
    assert "Reply 'submit'" in r.outbound.text and not tools.submitted and r.state.open_question.field == "final_review"
    r = await say(rt, s.id, "submit")
    assert tools.submitted[0]["answers"]["full_name"] == "Jane Doe"
    assert r.outbound.end_of_conversation and r.outbound.exit_reason == "completed"
    assert r.state.completed and all(v.status == "complete" for v in r.state.steps.values())
    assert any(i["type"] == "resume_token" for i in r.outbound.items)
    r = await say(rt, s.id, "anything else?")
    assert r.route == "knowledge" or "already completed" in r.outbound.text  # a question after the end is answered


async def fill_form_tail(rt, sid):
    return await say(rt, sid, "123 Main St, Austin TX 78701")


async def test_submit_only_from_candidate_action(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await fill_form(rt, s.id)
    assert r.state.open_question.field == "final_review" and not tools.submitted
    r = await say(rt, s.id, "looks good")  # not an explicit submit
    assert not tools.submitted and r.state.open_question.field == "final_review"
    r = await say(rt, s.id, "", action="submit_application")  # a submit button in the channel
    assert tools.submitted and r.state.completed


async def test_nodes_and_planner_cannot_trigger_actions(policy_yaml):
    from yachat.runtime.decide.tool_gateway import ActionRunner, ToolGateway
    from yachat.testing.fakes import FakeTools

    tools = FakeTools()
    gw = ToolGateway({"job_search", "submit_application"}, tools.impls())
    r = await gw.call("submit_application", {"job_id": "j1", "answers": {}})
    assert r.error_kind == "candidate_only" and not tools.submitted and gw.rejected == [("submit_application", "candidate_only")]
    runner = ActionRunner(tools.impls())
    r = await runner.run("submit_application", {"job_id": "j1", "answers": {}}, candidate_action=None)
    assert r.error_kind == "candidate_only" and not tools.submitted
    r = await runner.run("submit_application", {"job_id": "j1", "answers": {}}, candidate_action="submit_application")
    assert r.ok and tools.submitted
    ctx = make_context(policy=build(policy_yaml))
    assert "submit_application" not in planner_tools(ctx)  # the planner is never offered an action


async def test_drop_job_mid_form_reopens_job_search_keeps_answers(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    r = await say(rt, s.id, "actually I don't want that job")
    assert r.state.steps["job_search"].status == "in_progress" and r.state.steps["job_search"].reopen_count == 1
    assert "selected_job" not in r.state.outputs and r.state.fields["full_name"] == "Jane Doe"
    assert "Which job interests you" in r.outbound.text and not r.state.completed
    r = await say(rt, s.id, "2")
    assert r.state.outputs["selected_job"]["id"] == "j2"
    assert "home address" in r.outbound.text.lower() and "full name" not in r.outbound.text.lower()
    await say(rt, s.id, "9 Elm Street, Boston MA 02108")
    r = await say(rt, s.id, "submit")
    assert r.state.completed and tools.submitted[0]["job_id"] == "j2"


async def test_adaptive_detour_runs_planner_then_resumes(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    await say(rt, s.id, "yes")
    await say(rt, s.id, "engineer in austin")
    r = await say(rt, s.id, "which pays more and is closer to 78704?")
    assert r.route == ROUTE_PLANNER and rt.llm.calls_for("plan")  # the listed jobs are in the conversation: no lookup needed
    verifies = rt.llm.calls_for("verify_grounded")
    assert verifies and verifies[0].metadata["attempt"] == 1 and "$150k" in verifies[0].system  # grounded in the conversation
    assert "?" in r.outbound.text  # the pick question again
    assert r.state.digressions == 0 and r.state.open_question.field == "job_pick" and r.state.steps["job_search"].status == "in_progress"
    assert r.state.outputs.get("selected_job") is None  # the planner cannot complete a step or pick for the candidate
    r = await say(rt, s.id, "1")
    assert r.route == "form_fill" and r.state.outputs["selected_job"]["id"] == "j1"


async def test_verifier_rejects_unsupported_pay_rate():
    """The grounding verifier (real model) must reject a draft whose pay figure is not in the tool output."""
    tool_output = "job_search → [{'id': 'j1', 'title': 'Backend Engineer', 'location': 'Austin, TX 78701', 'pay': '$150k'}]"
    verdicts = {}
    for draft in ("The Backend Engineer role pays $999k.", "The Backend Engineer role pays $150k and is in Austin."):
        req = LLMRequest(
            "verify_grounded",
            ModelConfig.from_env().classify,
            verify_system([tool_output]),
            [{"role": "user", "content": f"Ask: which pays more?\nDraft: {draft}"}],
            max_tokens=200,
        )
        verdicts[draft] = await llm_from_env().structured(req, VERIFY_SCHEMA)
    assert not grounded(verdicts["The Backend Engineer role pays $999k."])
    assert grounded(verdicts["The Backend Engineer role pays $150k and is in Austin."]), verdicts


async def test_plain_answer_never_routes_to_planner(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    r = await say(rt, s.id, "my address is 12 Elm St, Austin TX 78704")
    assert r.route == "form_fill" and not rt.llm.calls_for("plan") and "12 Elm St" in r.state.fields["address"]


async def test_off_goal_comparisons_nudge_then_disengage(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "which is better, the gym and the cafeteria?")
    await say(rt, s.id, "which has more parking and a bigger gym?")
    r = await say(rt, s.id, "which cafeteria is nicer and closer?")
    assert r.state.nudges == 1 and "back on track" in r.outbound.text and not rt.llm.calls_for("plan")
    r = await say(rt, s.id, "which gym is better and cheaper?")
    assert r.state.exited and r.outbound.exit_reason == "disengaged"


async def test_flow_mode_never_runs_planner(policy_yaml):
    rt, _ = await make_runtime(mutate(policy_yaml, lambda d: d.update(mode="flow")))
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    await say(rt, s.id, "yes")
    await say(rt, s.id, "engineer in austin")
    r = await say(rt, s.id, "which pays more and is closer to 78704?")
    assert not rt.llm.calls_for("plan") and r.route != ROUTE_PLANNER and r.state.digressions == 0  # on-step enquiry: answered, not counted


async def test_planner_cannot_call_tool_outside_allow_list(policy_yaml):
    ctx = make_context(policy=build(policy_yaml))
    assert "salary_lookup" not in planner_tools(ctx) and set(planner_tools(ctx)) <= set(ctx.policy.allowed_tools)
    r = await ctx.services.gateway.call("salary_lookup", {"title": "x"})
    assert r.error_kind == "not_allowed"


async def test_career_coach_open_dialog_and_scope():
    policy = (ROOT / "examples" / "career_coach.yaml").read_text()
    rt, tools = await make_runtime(policy)
    s = await rt.start_session("career-coach", "u1")
    r = await say(rt, s.id, "I want to move from support into data analysis")
    assert tools.calls["skills_search"] and tools.calls["role_search"] and r.route == ROUTE_PLANNER and r.state.digressions == 0
    before = len(tools.calls["skills_search"])
    r = await say(rt, s.id, "can you review my lease?")
    assert "outside" in r.outbound.text.lower() or "scope" in r.outbound.text.lower(), r.outbound.text
    assert r.state.digressions == 1 and len(tools.calls["skills_search"]) == before


async def test_new_query_at_pick_stage_searches_again(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    await say(rt, s.id, "yes")
    await say(rt, s.id, "engineer in austin")
    r = await say(rt, s.id, "busco trabjo d enfermera en austin")
    role = tools.calls["job_search"][1]["role"].lower()
    assert len(tools.calls["job_search"]) == 2 and ("enfermera" in role or "nurs" in role)  # shorthand expanded, sometimes translated
    assert r.state.open_question.field == "job_pick" and "job_pick" not in r.state.reasks


async def test_opening_message_can_carry_consent_and_prefill(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    r = await say(rt, s.id, "Yes, I consent. I live in Austin")
    assert r.state.consent is True and "role and location" in r.outbound.text.lower() and r.state.prefilled.get("location") == "Austin"
    r = await say(rt, s.id, "backend engineer")
    assert "austin" in r.outbound.text.lower() and "(yes/no)" in r.outbound.text  # prefilled location is confirmed, not assumed


async def test_a_failed_answer_asks_the_open_question_once(policy_yaml, monkeypatch):
    from yachat.capabilities.nodes.knowledge import Knowledge
    from yachat.runtime.respond.rewriter import Rewriter

    async def down(self, ctx, config):
        raise RuntimeError("knowledge down")

    async def plain(self, ctx):
        return ctx

    monkeypatch.setattr(Knowledge, "act", down)
    monkeypatch.setattr(Rewriter, "run", plain)  # assert on the plain reply the orchestrator built
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    await say(rt, s.id, "yes")
    await say(rt, s.id, "backend engineer in austin")
    r = await say(rt, s.id, "What benefits does the company offer?")
    assert r.route == "knowledge"
    assert r.outbound.text.count(r.state.open_question.ask) == 1

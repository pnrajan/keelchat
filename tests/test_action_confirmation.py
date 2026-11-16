"""A confirmed business action runs once, as shown: "submit" is bound to the exact application the user saw, within a
window, and a reply that no longer matches, has expired, was already used, or whose outcome is unknown never sends
it again. Control flow only: the form runs on the real model, the action on the fake."""

from __future__ import annotations

from datetime import timedelta

import pytest

from keelchat.state.models import now
from keelchat.testing.fakes import FakeTools
from tests.helpers import fill_form, make_runtime, reach_form, say

pytestmark = pytest.mark.smoke  # part of the short real-model set: `uv run pytest -m smoke`


async def _at_review(policy_yaml: str, tools: FakeTools):
    rt, _ = await make_runtime(policy_yaml, tools=tools)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await fill_form(rt, s.id)
    assert "submit" in r.outbound.text.lower() and r.state.pending_action is not None and r.state.pending_action.status == "pending"
    return rt, s.id


async def test_submit_sends_exactly_what_was_shown_once_with_a_durable_operation_id(policy_yaml):
    tools = FakeTools()
    rt, sid = await _at_review(policy_yaml, tools)
    st = await rt.sessions.load(sid)
    op = st.pending_action.id
    r = await say(rt, sid, "submit")
    assert len(tools.submitted) == 1 and tools.submitted[0]["operation_id"] == op and r.state.pending_action.status == "done"
    assert r.state.pending_action.result_id == "app_1" and r.state.completed
    await say(rt, sid, "submit")  # a replayed confirmation after completion
    assert len(tools.submitted) == 1


async def test_a_confirmation_for_different_arguments_shows_the_review_again_instead_of_sending(policy_yaml):
    tools = FakeTools()
    rt, sid = await _at_review(policy_yaml, tools)
    st = await rt.sessions.load(sid)
    st.pending_action.args_hash = "not-what-is-shown-now"  # the application changed under the confirmation
    await rt.sessions.save(st)
    r = await say(rt, sid, "submit")
    assert not tools.submitted and "submit" in r.outbound.text.lower() and r.state.pending_action.status == "pending", r.outbound.text
    r = await say(rt, sid, "submit")  # the fresh review bound a fresh confirmation
    assert len(tools.submitted) == 1 and r.state.completed


async def test_an_expired_confirmation_is_rejected_and_rebound(policy_yaml):
    tools = FakeTools()
    rt, sid = await _at_review(policy_yaml, tools)
    st = await rt.sessions.load(sid)
    st.pending_action.expires_at = now() - timedelta(seconds=1)
    await rt.sessions.save(st)
    r = await say(rt, sid, "submit")
    assert not tools.submitted and "submit" in r.outbound.text.lower() and not r.state.pending_action.expired()


async def test_an_unknown_outcome_is_never_resent_on_its_own(policy_yaml):
    tools = FakeTools()
    rt, sid = await _at_review(policy_yaml, tools)
    st = await rt.sessions.load(sid)
    st.pending_action.status = "unknown"  # a timeout whose result nobody saw
    await rt.sessions.save(st)
    r = await say(rt, sid, "submit")
    assert not tools.submitted and "won't send it again" in r.outbound.text.lower().replace("’", "'") or not tools.submitted
    assert r.state.pending_action.status == "unknown" and r.state.completed


async def test_a_clear_failure_shows_the_review_again_and_a_fresh_submit_resends(policy_yaml):
    tools = FakeTools(fail={"submit_application"})
    rt, sid = await _at_review(policy_yaml, tools)
    r = await say(rt, sid, "submit")
    assert len(tools.calls["submit_application"]) == 1 and not tools.submitted and r.state.pending_action.status == "pending"
    assert "submit" in r.outbound.text.lower() and "submit_application: upstream" in r.errors
    tools.fail.clear()
    r = await say(rt, sid, "submit")
    assert len(tools.submitted) == 1 and r.state.completed


async def test_a_review_without_a_bound_confirmation_asks_to_confirm_without_claiming_a_change(policy_yaml):
    """A session that reached the review before confirmations were bound has no pending action: it is asked to confirm
    this version, not told the application changed."""
    tools = FakeTools()
    rt, sid = await _at_review(policy_yaml, tools)
    st = await rt.sessions.load(sid)
    st.pending_action = None
    await rt.sessions.save(st)
    r = await say(rt, sid, "submit")
    assert not tools.submitted and "submit" in r.outbound.text.lower() and r.state.pending_action is not None
    assert "changed" not in r.outbound.text.lower() and "expired" not in r.outbound.text.lower(), r.outbound.text
    r = await say(rt, sid, "submit")
    assert len(tools.submitted) == 1 and r.state.completed

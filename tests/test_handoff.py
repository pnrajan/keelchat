from __future__ import annotations

import pytest

from keelchat.capabilities.nodes.common import is_yes
from keelchat.capabilities.preprocessors.handoff_request import asks_for_person
from keelchat.policy.compiler import build
from keelchat.policy.errors import PolicyValidationError
from keelchat.registry.contract import make_context
from keelchat.runtime.decide.planner import planner_tools
from keelchat.state.store import InMemoryHandoffStore
from keelchat.testing.fakes import FakeTools
from tests.helpers import make_runtime, mutate, reach_form, say


def with_handoff(policy_yaml: str, **spec: object) -> str:
    return mutate(policy_yaml, lambda d: d.update(handoff=spec or {}))


def test_plain_asks_for_a_person_are_recognised():
    for text in ("Can I talk to a human?", "I want to speak with a real person", "human please", "quiero hablar con una persona", "live agent"):
        assert asks_for_person(text), text
    for text in ("insurance agent in austin", "Jane Doe", "what do humans do here?", "123 Main St"):
        assert not asks_for_person(text), text


def test_the_notify_tool_is_optional_checked_at_build_and_kept_from_the_planner(policy_yaml):
    inbox_only = build(with_handoff(policy_yaml))
    assert inbox_only.handoff is not None and inbox_only.handoff.notify is None and "handoff_webhook" not in inbox_only.allowed_tools
    p = build(with_handoff(policy_yaml, notify="handoff_webhook", when=["user_request", "max_reasks"]))
    assert p.handoff is not None and p.handoff.notify == "handoff_webhook" and "handoff_webhook" in p.allowed_tools
    assert build(policy_yaml).hash != p.hash and build(policy_yaml).handoff is None
    ctx = make_context(policy=p)
    assert "handoff_webhook" not in planner_tools(ctx)  # a write tool: only the handoff step sends handoffs
    for bad, path in (({"notify": "job_search"}, "handoff.notify"), ({"notify": "nope"}, "handoff.notify"), ({"when": ["whenever"]}, "handoff.when")):
        with pytest.raises(PolicyValidationError) as e:
            build(with_handoff(policy_yaml, **bad))
        assert any(err.path.startswith(path) for err in e.value.errors), (bad, [x.path for x in e.value.errors])


async def test_asking_for_a_person_lands_in_the_inbox_and_later_messages_join_it(policy_yaml):
    rt, tools = await make_runtime(with_handoff(policy_yaml))
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "Can I talk to a human please?")
    assert r.route == "handoff" and r.outbound.exit_reason == "handed_off" and r.state.exit_reason == "handed_off"
    assert not tools.calls["handoff_webhook"]  # no notify tool: the platform inbox only
    (h,) = await rt.handoffs.list("default", status="open")
    assert h.reason == "user_request" and h.session_id == s.id and h.agent == "hiring-agent" and h.fields["role"]
    assert h.transcript[-1].role == "user" and h.transcript[-1].text == "Can I talk to a human please?"
    assert {"type": "handoff", "ticket_id": h.id, "reason": "user_request"} in r.outbound.items
    r = await say(rt, s.id, "My name is Jane, I'm still here")
    h = await rt.handoffs.get("default", h.id)
    assert h is not None and h.transcript[-1].text == "My name is Jane, I'm still here"
    assert r.route == "handoff" and "full_name" not in r.state.fields  # the bot no longer works the flow
    opened = await rt.audit.query("default", session_id=s.id, kinds=["handoff"])
    assert [e.decision for e in reversed(opened)] == ["opened", "follow_up"] and opened[-1].ref == h.id


def test_go_ahead_is_a_yes():
    assert is_yes("Go ahead.") and is_yes("sure, go ahead") and not is_yes("go away")


async def test_consent_and_an_ask_for_a_person_in_one_message_hands_off(policy_yaml):
    rt, _ = await make_runtime(with_handoff(policy_yaml))
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    r = await say(rt, s.id, "Sure, go ahead. Now can I speak to a real person instead?")
    assert r.state.consent is True and r.state.exit_reason == "handed_off", r.outbound.text
    (h,) = await rt.handoffs.list("default")
    assert h.reason == "user_request"


async def test_no_and_an_ask_for_a_person_ends_as_declined(policy_yaml):
    rt, _ = await make_runtime(with_handoff(policy_yaml))
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    r = await say(rt, s.id, "No, you can't use my data. I want to talk to a human.")
    assert r.state.consent is False and r.state.exit_reason == "declined" and not await rt.handoffs.list("default")


async def test_a_notify_tool_also_tells_the_helpdesk(policy_yaml):
    rt, tools = await make_runtime(with_handoff(policy_yaml, notify="handoff_webhook"))
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Can I talk to a human please?")
    (h,) = await rt.handoffs.list("default")
    (call,) = tools.calls["handoff_webhook"]
    assert call["reason"] == "user_request" and call["ticket_id"] == h.id and call["session_id"] == s.id and call["fields"]["role"]
    await say(rt, s.id, "still here")
    follow = tools.calls["handoff_webhook"][1]
    assert follow["reason"] == "follow_up" and follow["ticket_id"] == h.id and follow["transcript"] == [{"role": "user", "text": "still here"}]


async def test_without_a_handoff_policy_the_flow_carries_on(policy_yaml):
    rt, tools = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "Can I talk to a human please?")
    assert not await rt.handoffs.list("default") and not r.state.exited and r.state.open_question.field == "full_name"


async def test_no_handoff_before_consent(policy_yaml):
    rt, tools = await make_runtime(with_handoff(policy_yaml))
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    r = await say(rt, s.id, "I want to talk to a real person")
    assert not await rt.handoffs.list("default") and not r.state.exited and r.state.open_question.field == "consent"
    (declined,) = await rt.audit.query("default", session_id=s.id, kinds=["handoff"])
    assert declined.decision == "declined" and declined.reason == "no_consent"


async def test_a_failed_notify_still_hands_off_and_is_audited(policy_yaml):
    rt, tools = await make_runtime(with_handoff(policy_yaml, notify="handoff_webhook"), tools=FakeTools(fail={"handoff_webhook"}))
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "talk to a human")
    assert tools.calls["handoff_webhook"] and r.state.exit_reason == "handed_off"
    assert len(await rt.handoffs.list("default", status="open")) == 1
    failed = await rt.audit.query("default", session_id=s.id, kinds=["handoff"])
    assert "notify_failed" in [e.decision for e in failed]


async def test_when_the_inbox_is_down_the_conversation_carries_on(policy_yaml):
    class DownInbox(InMemoryHandoffStore):
        async def create(self, handoff):
            raise ConnectionError("inbox down")

    rt, _ = await make_runtime(with_handoff(policy_yaml), handoffs=DownInbox())
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "talk to a human")
    assert not r.state.exited and r.state.open_question.field == "full_name" and "handoff" not in r.state.data


async def test_a_listed_dead_end_goes_to_a_person_instead_of_ending(policy_yaml):
    rt, tools = await make_runtime(with_handoff(policy_yaml, when=["max_reasks"]))
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    await say(rt, s.id, "my address is downtown")
    r = await say(rt, s.id, "my address is near the park")
    (h,) = await rt.handoffs.list("default")
    assert h.reason == "max_reasks" and h.fields["full_name"] == "Jane Doe"
    assert r.outbound.exit_reason == "handed_off" and r.state.data["handoff"]["reason"] == "max_reasks"


async def test_an_unlisted_dead_end_still_exits(policy_yaml):
    rt, tools = await make_runtime(with_handoff(policy_yaml))  # user_request only
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    await say(rt, s.id, "Jane Doe")
    await say(rt, s.id, "my address is downtown")
    r = await say(rt, s.id, "my address is near the park")
    assert not await rt.handoffs.list("default") and r.outbound.exit_reason == "max_reasks"


async def _handed_off(policy_yaml: str, **spec: object):  # type: ignore[no-untyped-def]
    from httpx import ASGITransport, AsyncClient

    from keelchat.api.app import create_app
    from keelchat.state.store import InMemoryTenantToolStore

    rt, tools = await make_runtime(with_handoff(policy_yaml, **spec))
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "Can I talk to a human please?")
    assert r.state.exit_reason == "handed_off"
    (h,) = await rt.handoffs.list("default", status="open")
    client = AsyncClient(transport=ASGITransport(app=create_app(rt, tool_store=InMemoryTenantToolStore())), base_url="http://t")
    return rt, tools, s, h, client


async def test_the_receiver_reports_back_on_its_callback_and_the_conversation_resumes(policy_yaml):
    from urllib.parse import parse_qs, urlparse

    rt, tools, s, h, c = await _handed_off(policy_yaml, notify="handoff_webhook", when=["user_request", "max_reasks"])
    (sent,) = tools.calls["handoff_webhook"]
    url = urlparse(sent["callback_url"])
    assert url.path == f"/v1/public/handoffs/default/{h.id}/callback"
    token = parse_qs(url.query)["token"][0]
    async with c:
        assert (await c.post(url.path, params={"token": "wrong"}, json={"status": "resolved"})).status_code == 404
        assert (await c.post(url.path, params={"token": token}, json={"status": "claimed"})).status_code == 422  # who claims it
        bad = await c.post(url.path, params={"token": token}, json={"status": "resolved", "fields": {"shoe_size": "9"}})
        assert bad.status_code == 422 and (await rt.session_state(s.id)).exit_reason == "handed_off"  # nothing changed
        got = await c.post(url.path, params={"token": token}, json={"status": "resolved", "fields": {"full_name": "Jane Doe"}, "note": "called her"})
        assert got.json() == {"id": h.id, "status": "resolved", "conversation": "resumed"}
        again = await c.post(url.path, params={"token": token}, json={"status": "resolved"})
        assert again.json()["conversation"] == "unchanged"  # reported twice: the second changes nothing
        late = await c.post(url.path, params={"token": token}, json={"status": "claimed", "assignee": "bot-retry"})
        assert late.json()["status"] == "resolved"  # resolved is final
        listed = (await c.get("/v1/tenants/default/handoffs")).json()
        assert "callback_hash" not in listed[0] and listed[0]["note"] == "called her"
    st = await rt.session_state(s.id)
    assert not st.exited and st.exit_reason is None and "handoff" not in st.data
    assert st.prefilled["full_name"] == "Jane Doe" and "full_name" not in st.fields  # offered back, not trusted unchecked
    assert [t.text for t in st.recent_turns()] == []  # the talk before the handoff was the person's to handle
    r = await say(rt, s.id, "ok thanks")
    assert r.route != "handoff" and r.state.open_question is not None and r.state.open_question.field.startswith("full_name")
    assert "Jane Doe" in r.outbound.text  # confirms the person's answer instead of asking from scratch
    assert r.state.digressions == 0 and "resumed" not in r.state.data  # "ok thanks" was for the person: nothing counted
    events = await rt.audit.query("default", session_id=s.id, kinds=["handoff"])
    assert {"resumed", "resolved"} <= {e.decision for e in events}


async def test_resolving_in_the_inbox_resumes_by_default_or_ends_the_conversation(policy_yaml):
    rt, _, s, h, c = await _handed_off(policy_yaml)
    async with c:
        r = await c.post(f"/v1/tenants/default/handoffs/{h.id}", json={"status": "resolved", "resume": False, "assignee": "ana@team"})
        assert r.status_code == 200 and "callback_hash" not in r.json()
    assert (await rt.session_state(s.id)).exit_reason == "resolved_by_person"
    ended = await say(rt, s.id, "hello?")
    assert ended.outbound.text.startswith("This conversation has ended")

    rt, _, s, h, c = await _handed_off(policy_yaml)
    async with c:
        await c.post(f"/v1/tenants/default/handoffs/{h.id}", json={"status": "resolved", "assignee": "ana@team"})
    assert not (await rt.session_state(s.id)).exited


async def test_a_ticket_still_closes_when_its_conversation_has_expired(policy_yaml):
    rt, _, s, h, c = await _handed_off(policy_yaml)
    rt.sessions._s.pop(s.id)  # type: ignore[attr-defined]  # the in-memory store; Redis/Postgres expire it
    async with c:
        r = await c.post(f"/v1/tenants/default/handoffs/{h.id}", json={"status": "resolved", "assignee": "ana@team"})
    assert r.status_code == 200 and r.json()["status"] == "resolved"


async def test_with_no_report_back_the_assistant_takes_over_after_the_timeout(policy_yaml):
    from datetime import timedelta

    from keelchat.state.models import now

    rt, _, s, h, c = await _handed_off(policy_yaml, timeout="1h")
    await c.aclose()
    held = await say(rt, s.id, "still there?")
    assert held.route == "handoff"  # within the hour: still with the team
    st = await rt.session_state(s.id)
    st.data["handoff"]["opened_at"] = (now() - timedelta(hours=2)).isoformat()
    await rt.sessions.save(st)
    r = await say(rt, s.id, "Jane Doe")
    assert r.route != "handoff" and not r.state.exited
    assert "timed_out" in {e.decision for e in await rt.audit.query("default", session_id=s.id, kinds=["handoff"])}
    assert (await rt.handoffs.get("default", h.id)).status == "open"  # the ticket stays for the team to see

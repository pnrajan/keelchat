from __future__ import annotations

import pytest

from tests.helpers import make_runtime, mutate, reach_form, say
from yachat.capabilities.nodes.common import is_yes
from yachat.capabilities.preprocessors.handoff_request import asks_for_person
from yachat.policy.compiler import build
from yachat.policy.errors import PolicyValidationError
from yachat.registry.contract import make_context
from yachat.runtime.decide.planner import planner_tools
from yachat.state.store import InMemoryHandoffStore
from yachat.testing.fakes import FakeTools


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

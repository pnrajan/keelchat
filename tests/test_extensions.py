"""The example extension package (examples/extensions/booking_caps): a tool, a guardrail and a channel register from
outside the framework, pass the contract suite, and a policy uses them with no runtime change."""

from __future__ import annotations

import sys

import pytest

from keelchat.channels import CHANNELS
from keelchat.policy.compiler import build
from keelchat.registry.contract import check_capability
from keelchat.registry.core import REGISTRY
from keelchat.registry.discovery import discover_packages
from keelchat.runtime.context import OutboundMessage
from keelchat.runtime.decide.tool_gateway import ToolGateway
from tests.conftest import ROOT
from tests.helpers import make_runtime, say

BOOKING = (ROOT / "examples" / "booking_assistant.yaml").read_text()


@pytest.fixture(scope="module", autouse=True)
def booking_caps():
    sys.path.insert(0, str(ROOT / "examples" / "extensions"))
    assert discover_packages(["booking_caps"]) == ["booking_caps"]
    yield
    sys.path.remove(str(ROOT / "examples" / "extensions"))


async def test_the_package_registers_and_passes_the_contract_suite():
    assert REGISTRY.has_tool("slot_search") and "telegram" in CHANNELS
    await check_capability("tool", "slot_search", None)
    await check_capability("guardrail", "no_price_promises", None)
    r = await ToolGateway({"slot_search"}).call("slot_search", {"service": "Haircut", "day": "tuesday"})
    assert r.ok and r.data == {"slots": ["Tue 10:00", "Tue 15:30"]}
    assert not (await ToolGateway(set()).call("slot_search", {"service": "haircut"})).ok  # only where a policy allows it


async def test_the_guardrail_and_channel_do_their_jobs():
    guard = REGISTRY.guardrail("no_price_promises").cls()
    hit = await guard.check("A haircut is $45 on Tuesday.", None)  # type: ignore[arg-type]
    assert hit.flagged and hit.redacted == "A haircut is (price confirmed at the salon) on Tuesday."
    assert not (await guard.check("Tuesday at 10:00 is open.", None)).flagged  # type: ignore[arg-type]
    tg = CHANNELS["telegram"]
    msg = tg.normalize({"message": {"text": "hi", "chat": {"id": 42}, "message_id": 7}})
    assert (msg.text, msg.user_id, msg.channel_message_id) == ("hi", "tg:42", "7")
    out = tg.render(OutboundMessage("x" * 5000))
    assert len(out) == 2 and all(len(m.text) <= 4096 for m in out)


async def test_a_policy_uses_the_extension_with_the_real_model():
    """The whole booking: the form, then the package's own step looks slots up through the gateway, offers them, and
    takes the pick (validated in code)."""
    policy = build(BOOKING)
    assert {"slot_search"} <= set(policy.allowed_tools) and "no_price_promises" in policy.defaults.output_guardrails
    assert [s.node for s in policy.steps] == ["greet_consent", "form_fill", "pick_slot"]
    rt, _ = await make_runtime(BOOKING)
    s = await rt.start_session("booking-assistant", "client-1")
    for text in ("hi", "yes", "A haircut please", "Tuesday", "Ana Ruiz"):
        r = await say(rt, s.id, text)
    assert r.state.data.get("slots") == ["Tue 10:00", "Tue 15:30"], r.outbound.text  # looked up, offered
    assert r.state.open_question is not None and r.state.open_question.field == "slot", r.outbound.text
    r = await say(rt, s.id, "the second one")
    if not r.state.outputs.get("slot"):
        r = await say(rt, s.id, "2")
    assert r.state.outputs["slot"] == "Tue 15:30", r.outbound.text

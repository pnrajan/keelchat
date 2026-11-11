from __future__ import annotations

from keelchat.registry.core import REGISTRY, PreProcessor, preprocessor
from tests.helpers import make_runtime, say


def test_builtin_order():
    assert [p.name for p in REGISTRY.preprocessors()][:7] == [
        "receiver",
        "opt_out",
        "handoff_request",
        "language",
        "shorthand",
        "entity_normalization",
        "input_guardrails",
    ]


async def test_opt_out_text_on_web(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    r = await say(rt, s.id, "please stop messaging me")
    assert r.state.exited and r.state.exit_reason == "opted_out" and "won't receive" in r.outbound.text


async def test_entity_normalization(policy_yaml):
    rt, _ = await make_runtime(policy_yaml)
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello")
    r = await say(rt, s.id, "yes, call me at (512) 555-1234 on 03/05/2027")
    user_turn = [t for t in r.state.turns if t.role == "user"][-1]
    assert "+15125551234" in user_turn.text and "2027-03-05" in user_turn.text


async def test_contributed_preprocessor_runs_in_order(policy_yaml):
    @preprocessor("shout", order=35)
    class Shout(PreProcessor):
        async def run(self, ctx):
            ctx.text = ctx.text.upper()
            return ctx

    try:
        rt, _ = await make_runtime(policy_yaml)
        s = await rt.start_session("hiring-agent", "u1")
        r = await say(rt, s.id, "hello there")
        assert [t for t in r.state.turns if t.role == "user"][-1].text == "HELLO THERE"
        assert [t.stage for t in r.trace if t.stage.startswith("understand.")].index("understand.shout") == 5
    finally:
        REGISTRY._pre.pop("shout", None)

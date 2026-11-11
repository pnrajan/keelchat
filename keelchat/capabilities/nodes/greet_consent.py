from __future__ import annotations

from keelchat.capabilities.nodes.common import affirm, affirm_failed, answers_for, rule_affirm
from keelchat.registry.core import EmptyConfig, Node, NodeResult, node
from keelchat.runtime.context import TurnContext

ASK = (
    "consent",
    "Before we start: may I use your answers to help with this, and remember them for next time?",
    "yes or no",
)


@node("greet_consent", produces=["consent"])
class GreetConsent(Node[EmptyConfig]):
    """Greets the user and records consent to data use and memory."""

    async def act(self, ctx: TurnContext, config: EmptyConfig) -> NodeResult:
        answers = answers_for(ctx, "greet_consent")
        if not answers and ctx.state.open_question is None and rule_affirm(ctx.text) is not None:
            answers = {"consent": ctx.text}  # opening message already answers the consent question ("Yes, I consent. I live in Austin")
        if not answers:
            return NodeResult(
                f"Hi! {ASK[1]}",
                goal="Open with the greeting, then respond to anything they already said, then ask for consent to use their answers and remember them.",
                ask=ASK,
            )
        value = answers.get("consent") or next(iter(answers.values()))
        meant = await affirm(ctx, value, ASK[1])
        if meant == "yes":
            ctx.state.consent = True
            return NodeResult("Thanks.", goal="Thank them for agreeing, in a few words.", outputs={"consent": True}, complete=True)
        if meant == "no":
            ctx.state.consent = False
            return NodeResult("Understood, we won't process your data.", goal="Respect their no: say you won't use their data.", exit_reason="declined")
        if affirm_failed(ctx):  # could not read the reply: ask again, not counted
            return NodeResult(f"Sorry, I didn't catch that. {ASK[1]}", goal="Say you didn't catch that, and ask again.", ask=ASK)
        n = ctx.state.reasks.get("consent", 0) + 1
        ctx.state.reasks["consent"] = n
        if n >= ctx.policy.defaults.reasks:
            return NodeResult("I couldn't get a clear yes or no.", goal="Say you couldn't get a clear answer, so you'll stop here.", exit_reason="max_reasks")
        return NodeResult(f"Sorry, I need a yes or a no for this. {ASK[1]}", goal="Their answer was unclear: ask for a yes or a no.", ask=ASK)

"""Orchestrator: system prompt from the charter; flow / planner / adaptive routing; edits; runs the node."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from yachat.capabilities.nodes.handoff import HANDED_OFF, may_hand_off
from yachat.logging import get_logger
from yachat.registry.core import REGISTRY, NodeResult
from yachat.runtime.context import TurnContext
from yachat.runtime.decide import rules
from yachat.runtime.decide.intent_detector import DETECTOR_ERROR, GREETING_ONLY, ROUTE_NUDGE, ROUTE_PLANNER, ROUTE_REASK, SMALL_TALK, step_fields
from yachat.runtime.decide.planner import run_planner
from yachat.runtime.understand.language_rules import NAMES
from yachat.state.flow import StepGraph

log = get_logger("orchestrator")

NUDGE = {
    "en": "I'd love to help, but we need to finish a few questions first. Let's get back on track.",
    "es": "Me encantaría ayudar, pero primero necesitamos terminar unas preguntas. Volvamos al tema.",
}
OUT_OF_SCOPE_IN_FLOW = {
    "en": "That's outside what I can help with here, so let's get back to it.",
    "es": "Eso está fuera de lo que puedo hacer aquí, así que sigamos.",
}
OUT_OF_SCOPE = {
    "en": "That's outside what I can help with here. What would you like to do within that?",
    "es": "Eso está fuera de lo que puedo hacer aquí. ¿En qué te ayudo dentro de ese alcance?",
}
#: the first reply after a person hands the conversation back
BACK = {"en": "Thanks for waiting. You're back with the assistant.", "es": "Gracias por esperar. Vuelves a hablar con el asistente."}
#: small talk answered in kind: the reply's goal, by the policy's `small_talk`
SMALL_TALK_GOAL = {
    "brief": "Answer their small talk in kind, in one short friendly sentence, as the assistant (asked how you are: say you're "
    "doing well and thank them; a remark: agree or react to it)",
    "warm": "Answer their small talk warmly, in one or two short sentences, as the assistant (asked how you are: say you're "
    "doing well, thank them and ask after them; a remark: react to it and pick up what they mentioned)",
}
#: what a reply does when it answers a side question in the middle of a step
RETURN_GOAL = "Then return to the open question and ask it."


def system_prompt(ctx: TurnContext) -> str:
    parts = [f"You are {ctx.policy.agent}.", f"Charter: {ctx.policy.charter.strip()}", f"Reply in {NAMES.get(ctx.language, ctx.language)}."]
    public = [f"{m.item}: {m.value}" for m in ctx.memory if not m.sensitive]
    if public:
        parts.append("Known about the user: " + "; ".join(public))
    if ctx.rules:
        parts.append(rules.instructions(ctx.rules))
    return "\n".join(parts)


def owner_of_field(ctx: TurnContext, field: str) -> str | None:
    for s in ctx.policy.steps:
        if field in step_fields(ctx, s.name):
            return s.name
    return None


class Orchestrator:
    name = "orchestrator"

    async def _run_node(self, ctx: TurnContext, name: str) -> NodeResult:
        spec = REGISTRY.node(name)
        settings = ctx.policy.step(name).settings if any(s.name == name for s in ctx.policy.steps) else {}
        cfg = spec.config_model.model_validate(settings)
        services, models = ctx.services, rules.models_for(ctx, name)
        if models is not services.models:  # the step's tier, while it acts
            ctx.services = replace(services, models=models)
        try:
            return await asyncio.wait_for(spec.cls().act(ctx, cfg), timeout=spec.cls.timeout_s)
        except Exception as e:  # isolation: a failing node yields a graceful reply
            log.error("node failed", node=name, error=f"{e.__class__.__name__}: {e}")
            ctx.errors.append(f"node {name}: {e.__class__.__name__}")
            q = ctx.state.open_question
            return NodeResult("Something went wrong on my side. " + (q.ask if q else "Let's try again."))
        finally:
            ctx.services = services

    def _apply(self, ctx: TurnContext, graph: StepGraph, name: str, r: NodeResult) -> None:
        st = ctx.state
        if r.ask is not None:
            graph.set_open(st, name, *r.ask, free_text=r.free_text)
        st.outputs.update(r.outputs)
        if r.complete and name in st.steps:
            step = ctx.policy.step(name)
            if graph.outputs_valid(st, step) or not step.produces:
                graph.complete(st, name)
                log.info("step complete", step=name)
        ctx.guard.step = name
        ctx.guard.field = st.open_question.field if st.open_question else None
        ctx.guard.allowed_pii |= r.allowed_pii

    async def _end(self, ctx: TurnContext, reason: str) -> NodeResult:
        """A dead end the policy lists under `handoff.when` goes to a person; otherwise (or when that fails) the exit step."""
        ctx.exit_reason = reason
        h = ctx.policy.handoff
        if h is not None and reason in h.when and may_hand_off(ctx):
            ctx.handoff_reason = reason
            r = await self._run_node(ctx, "handoff")
            if ctx.state.exit_reason == HANDED_OFF:
                return r
            ctx.exit_reason = reason
        return await self._run_node(ctx, "exit")

    def _next(self, ctx: TurnContext, graph: StepGraph) -> str:
        nxt = graph.next_step(ctx.state)
        if nxt is None:
            ctx.exit_reason = "completed"
            return "exit"
        graph.start(ctx.state, nxt)
        return nxt

    def _apply_edit(self, ctx: TurnContext, graph: StepGraph) -> str | None:
        """`edit`: update the saved field and reopen the step that owns it (and completed dependents keep their answers)."""
        v, st = ctx.verdict, ctx.state
        assert v is not None
        field = v.edited_field or next(iter(v.fields), None)
        if not field or field not in v.fields:
            return None
        owner = owner_of_field(ctx, field)
        st.fields[field] = v.fields[field]
        st.prefilled.pop(field, None)
        ctx.validated_answers[field] = v.fields[field]
        if owner is None:
            return None
        for out in ctx.policy.step(owner).produces:
            st.outputs.pop(out, None)
        if st.step(owner).status == "complete" and not graph.reopen(st, owner):
            ctx.exit_reason = "max_reopens"
            return "exit"
        graph.start(st, owner)
        st.open_question = None
        st.data["edited_field"] = field
        log.info("edit applied", field=field, reopened=owner)
        return owner

    async def run(self, ctx: TurnContext) -> TurnContext:
        resumed = ctx.state.data.get("resumed", False)  # the first turn back from a person (cleared after the turn)
        ctx = await self._route(ctx)
        r = ctx.node_result
        if resumed and r is not None and not ctx.handoff_reason:
            r.text = f"{BACK.get(ctx.language, BACK['en'])}\n{r.text}"
            r.goal = "Say they are back with the assistant. Then " + (r.goal or "say the plain version.")
        return ctx

    async def _route(self, ctx: TurnContext) -> TurnContext:
        ctx.system_prompt = system_prompt(ctx)
        st, graph, q = ctx.state, StepGraph(ctx.policy), ctx.state.open_question

        if ctx.handoff_reason:  # the user asked for a person, or already has one
            consent_r = None
            if ctx.handoff_reason != "follow_up" and not may_hand_off(ctx) and q and q.field == "consent" and ctx.verdict and "consent" in ctx.verdict.fields:
                consent_r = await self._run_node(ctx, q.step)  # "yes, and let me talk to a person": the consent answer counts first
                self._apply(ctx, graph, q.step, consent_r)
                if consent_r.exit_reason is not None:  # they said no
                    ex = await self._end(ctx, consent_r.exit_reason)
                    ctx.route, ctx.node_result = (
                        "exit",
                        NodeResult(f"{consent_r.text}\n{ex.text}", goal=f"{consent_r.goal} Then {ex.goal}", exit_reason=ex.exit_reason),
                    )
                    return ctx
            ctx.route = "handoff"
            log.info("routing", decision="handoff", reason=ctx.handoff_reason)
            r = await self._run_node(ctx, "handoff")
            if consent_r is not None:
                r = NodeResult(f"{consent_r.text}\n{r.text}", goal=f"{consent_r.goal} Then {r.goal}", items=r.items, exit_reason=r.exit_reason)
            ctx.node_result = r
            q = st.open_question
            ctx.guard.step, ctx.guard.field = (q.step, q.field) if q and not st.exited else (None, None)
            return ctx

        if not ctx.policy.has_flow:  # planner mode, no flow
            if ctx.verdict and ctx.verdict.reason == SMALL_TALK:
                ctx.reply("Happy to chat! What can I help you with?", goal=SMALL_TALK_GOAL[ctx.policy.small_talk] + ", then ask what you can help with.")
                return ctx
            if ctx.verdict and not ctx.verdict.in_scope:
                log.info("routing", decision="decline", reason="out of scope per charter")
                ctx.reply(
                    OUT_OF_SCOPE.get(ctx.language, OUT_OF_SCOPE["en"]), goal="Say kindly that this is outside what you help with, and what you can help with."
                )
                return ctx
            ctx.route = ROUTE_PLANNER
            log.info("routing", decision="planner", reason="open-dialog agent")
            ctx.node_result = await run_planner(ctx)
            return ctx

        if ctx.route == ROUTE_PLANNER:
            r = await run_planner(ctx)
            if ctx.reask and q:
                r.text = f"{r.text}\n{q.ask}"
                r.goal = (r.goal + " " if r.goal else "") + RETURN_GOAL
            ctx.node_result = r
            ctx.guard.step, ctx.guard.field = (q.step, q.field) if q else (None, None)
            return ctx
        if ctx.route == ROUTE_NUDGE:
            ctx.reply(
                NUDGE.get(ctx.language, NUDGE["en"]) + (f"\n{q.ask}" if q else ""),
                goal="Say you'll gladly help with that once a few questions are done, then ask the question.",
            )
            return ctx
        if ctx.route == ROUTE_REASK:
            v = ctx.verdict
            if v is not None and v.reason == DETECTOR_ERROR:
                lead, goal = "Sorry, I didn't catch that.", "Say you didn't catch that, and ask the question again."
            elif v is not None and v.reason == GREETING_ONLY:
                lead, goal = "Hi again!", "Greet them back warmly in a few words, then ask the question again."
            elif v is not None and v.reason == SMALL_TALK:
                lead, goal = "Happy to chat!", SMALL_TALK_GOAL[ctx.policy.small_talk] + ", then ask the question again."
            elif v is not None and (not v.in_scope or v.intent == "off_topic"):
                lead = OUT_OF_SCOPE_IN_FLOW.get(ctx.language, OUT_OF_SCOPE_IN_FLOW["en"])
                goal = "Say briefly that this is outside what you help with here, then ask the question."
            else:
                lead, goal = "Let's get back to it.", "Respond to what they said in a few words, then gently ask the question."
            ctx.reply(lead + (f"\n{q.ask}" if q else ""), goal=goal)
            return ctx

        if ctx.verdict and ctx.verdict.conflict and ctx.route is None:
            field, values = ctx.verdict.conflict
            ctx.reply(
                f"I got two different values for {field.replace('_', ' ')}: {' and '.join(values)}. Which one is right?",
                goal=f"They gave two values for their {field.replace('_', ' ')}; ask which one is right.",
            )
            log.info("routing", decision="clarify_conflict", reason=f"two values for {field}")
            return ctx
        if st.completed and ctx.route is None:  # after a committed candidate action, edits are follow-ups
            if ctx.verdict and ctx.verdict.intent == "edit" and ctx.verdict.fields:
                st.data.setdefault("followups", []).append({"turn_id": ctx.turn_id, "fields": dict(ctx.verdict.fields)})
                log.info("follow-up recorded", fields=sorted(ctx.verdict.fields))
                ctx.reply(
                    "Noted. Your application is already submitted, so I've recorded this as a follow-up for the employer.",
                    goal="Confirm the change is recorded as a follow-up, since the application was already sent.",
                )
            elif ctx.verdict and ctx.verdict.intent == "enquiry" and ctx.services.gateway.is_allowed("knowledge_search"):
                ctx.route = "knowledge"  # a question after the end still deserves an answer
                ctx.node_result = await self._run_node(ctx, "knowledge")
            else:
                ctx.reply("You've already completed everything here, thank you.", goal="Say everything here is already done, and thank them.")
            return ctx
        route = ctx.route
        if route is None and ctx.verdict:
            if ctx.verdict.intent == "edit":
                route = self._apply_edit(ctx, graph)
                ctx.verdict.fields = {f: v for f, v in ctx.verdict.fields.items() if f != ctx.verdict.edited_field}
            if ctx.verdict.clear_outputs:
                reopened = [s for o in ctx.verdict.clear_outputs if (s := graph.clear_output(st, o))]
                if reopened:
                    log.info("outputs cleared", outputs=ctx.verdict.clear_outputs, reopened=reopened)
                    st.open_question = None
                    ctx.verdict.fields = {}
        route = route or self._next(ctx, graph)
        ctx.route = route
        log.info("routing", decision=route, reason="next ready step" if route != "exit" else "every step complete")
        r = await (self._end(ctx, ctx.exit_reason or "completed") if route == "exit" else self._run_node(ctx, route))
        self._apply(ctx, graph, route, r)
        if ctx.reask and route == "knowledge" and st.open_question and st.open_question.ask not in r.text:
            r.text = f"{r.text}\n{st.open_question.ask}"
            r.goal = (r.goal + " " if r.goal else "") + RETURN_GOAL
        texts, items, verbatim, allowed = [r.text], list(r.items), list(r.verbatim), set(r.allowed_pii)
        goals = [r.goal]
        if ctx.verdict and ctx.verdict.enquiry_text and ctx.verdict.verdict == "answered" and route != "knowledge":
            # an answer and an enquiry in one message: the reply covers both
            if ctx.services.gateway.is_allowed("knowledge_search"):
                saved_text, ctx.text = ctx.text, ctx.verdict.enquiry_text
                k = await self._run_node(ctx, "knowledge")
                ctx.text = saved_text
                texts.insert(0, k.text)
                goals.insert(0, k.goal)
                items = k.items + items
            log.info("enquiry answered alongside answer", reason="multi-intent message")
        hops = 0
        while r.exit_reason is None and r.complete and st.open_question is None and not st.exited and hops <= len(graph.order):
            hops += 1
            route = self._next(ctx, graph)
            r = await self._run_node(ctx, route)
            self._apply(ctx, graph, route, r)
            texts.append(r.text)
            goals.append(r.goal)
            items += r.items
            verbatim += r.verbatim
            allowed |= r.allowed_pii
        if r.exit_reason is not None and route != "exit":
            ex = await self._end(ctx, r.exit_reason)
            texts.append(ex.text)
            goals.append(ex.goal)
            items += ex.items
            r = ex
        ctx.route = route
        ctx.node_result = NodeResult(
            "\n".join(t for t in texts if t),
            goal=" Then ".join(g.rstrip(".") + "." for g in goals if g),
            items=items,
            exit_reason=r.exit_reason,
            allowed_pii=allowed,
            verbatim=verbatim,
        )
        ctx.guard.allowed_pii = allowed
        return ctx

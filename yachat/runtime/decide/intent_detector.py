"""Intent Detector, every turn (part of the Orchestrator): one structured call to a small model.

A message often carries several intents ("La segunda. ¿Tienen seguro médico?"). The detector returns a list; items are
processed in order edits → new answers → enquiries and the reply covers all of them. Counting lives here, in code.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from yachat.capabilities.nodes.handoff import may_hand_off
from yachat.capabilities.preprocessors.opt_out import CONFIRM_STOP, CONFIRM_STOP_KEY
from yachat.logging import get_logger
from yachat.registry.core import REGISTRY
from yachat.runtime.context import AnswerVerdict, IntentItem, StageTrace, TurnContext
from yachat.runtime.decide import rules
from yachat.runtime.llm import LLMRequest
from yachat.runtime.redaction import redact_pii
from yachat.state.flow import StepGraph
from yachat.state.models import OpenQuestion

log = get_logger("intent")

ROUTE_PLANNER = "__planner__"
ROUTE_NUDGE = "__nudge__"
DETECTOR_ERROR = "detector error"
GREETING_ONLY = "greeting"
#: a message that is only a greeting: never off-topic and never counted as wandering off (decided in code)
GREETING = re.compile(
    r"\s*(?:hi|hello|hey|hiya|howdy|yo|good (?:morning|afternoon|evening)|hola|buenos d[ií]as|buenas(?: tardes| noches)?|bonjour|salut|hallo|ol[aá])"
    r"(?:\s+(?:there|again|all|everyone))?[\s!.,]*",
    re.I,
)
ROUTE_REASK = "__reask__"
INTENTS = ("new_answer", "edit", "enquiry", "off_topic")
ORDER = {"edit": 0, "new_answer": 1, "enquiry": 2, "off_topic": 3}

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intents": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": list(INTENTS)},
                    "fields": {"type": "object", "additionalProperties": {"type": "string"}},
                    "edited_field": {"type": ["string", "null"]},
                    "needs_multi_step": {"type": "boolean"},
                    "on_step": {"type": "boolean", "description": "an enquiry about the current step itself (e.g. the jobs just offered)"},
                    "text": {"type": "string", "description": "the part of the message this item covers"},
                },
                "required": ["intent", "fields", "edited_field", "needs_multi_step", "on_step", "text"],
                "additionalProperties": False,
            },
        },
        "clear_outputs": {"type": "array", "items": {"type": "string"}},
        "in_scope": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["intents", "clear_outputs", "in_scope", "reason"],
    "additionalProperties": False,
}

#: asked only when the policy hands off on user_request, so other agents' prompts and schema stay as they are
WANTS_HUMAN = {
    "type": "boolean",
    "description": "the user's latest message asks to talk to a person (a human agent, a representative); an earlier ask does not count",
}

#: a quieter opt-out than the STOP words ("please don't message me again"): asked of every agent, confirmed before ending
WANTS_TO_STOP = {"type": "boolean", "description": "the user wants this conversation or these messages to stop for good"}

EXAMPLES_PATH = Path(__file__).with_name("intent_examples.yaml")


def asks_wants_human(ctx: TurnContext) -> bool:
    return ctx.policy.handoff is not None and "user_request" in ctx.policy.handoff.when


def schema_for(ctx: TurnContext) -> dict[str, Any]:
    extra: dict[str, Any] = {"wants_to_stop": WANTS_TO_STOP, **({"wants_human": WANTS_HUMAN} if asks_wants_human(ctx) else {})}
    if active := rules.active_rules(ctx):  # only agents with rules: other prompts and schemas stay as they are
        extra["rules"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string", "enum": [r.name for r in active]}, "quote": {"type": "string"}},
                "required": ["name", "quote"],
                "additionalProperties": False,
            },
        }
    return {**SCHEMA, "properties": {**SCHEMA["properties"], **extra}, "required": [*SCHEMA["required"], *extra]}


@lru_cache(maxsize=1)
def load_examples() -> dict[str, dict[str, list[dict[str, Any]]]]:
    data: dict[str, dict[str, list[dict[str, Any]]]] = yaml.safe_load(EXAMPLES_PATH.read_text(encoding="utf-8"))
    return data


def few_shot(language: str) -> str:
    """A handful of labeled examples per intent for the session language (English as fallback)."""
    per_lang = load_examples()
    examples = per_lang.get(language) or per_lang["en"]
    lines = [
        f'- asked "{ex["ask"]}", user "{ex["message"]}" -> {ex["intents"]}' + (f" clear_outputs={ex['clear_outputs']}" if ex.get("clear_outputs") else "")
        for items in examples.values()
        for ex in items
    ]
    return "\n".join(lines)


def step_fields(ctx: TurnContext, step: str) -> list[str]:
    return ctx.policy.step_fields(step)


def known_fields(ctx: TurnContext) -> list[str]:
    return ctx.policy.fields


def open_fields(ctx: TurnContext) -> list[str]:
    q = ctx.state.open_question
    if not q:
        return []
    pending = [f for f in step_fields(ctx, q.step) if f not in ctx.state.fields]
    return pending or [q.field]


def join_free_text(v: AnswerVerdict, q: OpenQuestion | None) -> AnswerVerdict:
    """A free-text answer given in two parts ("get your act together. don't promise what you can't keep") is one
    answer: the parts are joined, and nobody is asked which one is right."""
    if v.conflict is None or q is None or not q.free_text or v.conflict[0] != q.field:
        return v
    v.fields[q.field] = " ".join(p.strip().rstrip(".") for p in v.conflict[1])
    v.conflict = None
    return v


def summarize(items: list[IntentItem], message: str) -> AnswerVerdict:
    """Collapse the ordered list into the verdict the orchestrator consumes."""
    items = sorted(items, key=lambda i: ORDER.get(i.intent, 9))
    fields: dict[str, str] = {}
    conflict: tuple[str, list[str]] | None = None
    for it in items:
        if it.intent not in ("new_answer", "edit"):
            continue
        for f, v in it.fields.items():
            if f in fields and fields[f].strip().lower() != v.strip().lower():
                conflict = (f, [fields[f], v])
            fields.setdefault(f, v)
    answers = [i for i in items if i.intent in ("new_answer", "edit")]
    enquiries = [i for i in items if i.intent == "enquiry"]
    dominant = items[0].intent if items else "new_answer"
    verdict = AnswerVerdict(
        verdict="answered" if answers else "digressed",
        intent=dominant,
        fields=fields,
        edited_field=next((i.edited_field for i in items if i.intent == "edit"), None),
        items=items,
        enquiry_text=" ".join(i.text for i in enquiries).strip() or (message if enquiries else ""),
        needs_multi_step=any(i.needs_multi_step for i in enquiries),
        on_step=bool(enquiries) and all(i.on_step for i in enquiries),
        conflict=conflict,
    )
    return verdict


def form_fields(ctx: TurnContext) -> set[str]:
    """Fields the policy configures as free-text answers (a step's `fields` list)."""
    return {str(f["name"]) for st in ctx.policy.steps for f in (st.settings.get("fields") or [])}


class IntentDetector:
    name = "intent_detector"

    def _system(self, ctx: TurnContext) -> str:
        human = "\nSet wants_human only when the user asks to talk to a person rather than this assistant." if asks_wants_human(ctx) else ""
        stop = "\nSet wants_to_stop only when the user wants to end this conversation or stop all messages, not when they decline one question."
        active = rules.active_rules(ctx)
        return self._task(ctx) + human + stop + (rules.prompt_section(active) if active else "")

    def _task(self, ctx: TurnContext) -> str:
        q = ctx.state.open_question
        shots = few_shot(ctx.language)
        scope = f"Agent charter (scope): {ctx.policy.charter.strip()}\n"
        if q is None and not ctx.policy.has_flow:
            return scope + "Decide whether the user's message is within the charter's scope (in_scope). One item: enquiry when in scope, off_topic otherwise."
        saved = {k: v for k, v in ctx.state.fields.items() if v is not None}
        remembered = {m.item: m.value for m in ctx.memory if not m.sensitive}
        head = f"Current step: {q.step}. Open question: \"{q.ask}\" (field '{q.field}', expecting: {q.expect}). " if q else "No open question yet. "
        return (
            scope + head + f"Fields still open in this step: {open_fields(ctx)}. All fields in this flow: {known_fields(ctx)}. Saved answers: {saved}. "
            f"Remembered from earlier sessions: {remembered}.\n"
            "Return the list of intents in the message, one item per part: new_answer (answers an open or upcoming field, possibly several at "
            "once, even before asked), edit (changes a saved answer; set edited_field), enquiry (asks something; needs_multi_step only when "
            "it needs several lookups or comparisons; on_step only when it asks about the options or question this step just presented, e.g. the jobs "
            "just listed at a job pick; a general question while a name or address is being asked is not on_step), "
            "off_topic (unrelated to the charter). Put every field value found in `fields`; when one message gives several fields, fill each.\n"
            "Rules: extract only values stated in the latest user message, never from earlier turns. A short or partial reply that could be the "
            "requested value is new_answer; a question, or text that cannot be a value for the open field (e.g. 'remote?' when asked for a name), "
            "is enquiry or off_topic, never new_answer. If the message gives two different values for one field, return two new_answer items. "
            "List in clear_outputs any earlier result the user retracts (e.g. 'selected_job').\n"
            f"Examples:\n{shots}"
        )

    async def _call(self, ctx: TurnContext) -> AnswerVerdict:
        turns = [{"role": "user" if t.role == "user" else "assistant", "content": t.text} for t in ctx.state.recent_turns(6)]
        msgs: list[dict[str, str]] = []
        for m in [*turns, {"role": "user", "content": ctx.text}]:
            if msgs and msgs[-1]["role"] == m["role"]:
                msgs[-1]["content"] += "\n" + m["content"]
            else:
                msgs.append(dict(m))
        if msgs[0]["role"] != "user":
            msgs.insert(0, {"role": "user", "content": "(conversation start)"})
        q = ctx.state.open_question
        step = rules.asking_step(ctx)
        models = rules.tier(ctx.services.models, step.model if step else None)
        req = LLMRequest(
            "intent_detect",
            models.classify,
            self._system(ctx),
            msgs,
            max_tokens=400,
            effort=models.effort_classify,
            metadata={
                "open_field": q.field if q else None,
                "open_fields": open_fields(ctx),
                "known_fields": known_fields(ctx),
                "saved": dict(ctx.state.fields),
                "step": q.step if q else None,
            },
        )
        out = await ctx.services.llm.structured(req, schema_for(ctx))
        ctx.llm_calls += 1
        items = [
            IntentItem(
                intent=str(i.get("intent", "enquiry")),
                fields={str(k): str(x) for k, x in (i.get("fields") or {}).items()},
                edited_field=i.get("edited_field") or None,
                needs_multi_step=bool(i.get("needs_multi_step")),
                on_step=bool(i.get("on_step")),
                text=str(i.get("text", "")),
            )
            for i in out.get("intents") or []
        ]
        if not items:
            items = [IntentItem(intent="enquiry", text=ctx.text)]
        if q and q.field in form_fields(ctx):  # a free-text field has no options to ask about: a question here is never on-step
            for i in items:
                i.on_step = False
        for it in items:
            if it.intent == "new_answer" and not it.fields and q:
                it.fields = {q.field: it.text or ctx.text}
        v = join_free_text(summarize(items, ctx.text), ctx.state.open_question)
        v.clear_outputs = [str(x) for x in out.get("clear_outputs") or []]
        v.in_scope = bool(out.get("in_scope", True))
        v.reason = str(out.get("reason", ""))
        v.wants_human = bool(out.get("wants_human"))
        v.wants_to_stop = bool(out.get("wants_to_stop"))
        reported = [m for m in out.get("rules") or [] if isinstance(m, dict)]
        ctx.rules = rules.resolve(rules.grounded(reported, ctx.text), rules.active_rules(ctx))
        for r in ctx.rules:
            ctx.decide("rule", r.name, reason=r.when[:120])
        return v

    async def run(self, ctx: TurnContext) -> TurnContext:
        st, d = ctx.state, ctx.policy.defaults
        if ctx.handoff_reason and (ctx.handoff_reason == "follow_up" or may_hand_off(ctx)):  # passed to a person: nothing to classify
            ctx.trace.append(StageTrace("decide.intent_detector.llm", 0.0, skipped=True, note="handoff"))
            return ctx
        if not ctx.policy.has_flow:
            try:
                ctx.verdict = await asyncio.wait_for(self._call(ctx), timeout=d.intent_timeout_s)  # scope check only
            except Exception as e:  # fail open: a classifier hiccup never declines a user
                log.error("scope check failed; treating as in scope", error=f"{e.__class__.__name__}: {e}")
                ctx.errors.append(f"intent_detector: {e.__class__.__name__}")
                ctx.verdict = AnswerVerdict("digressed", intent="enquiry", reason="detector error", items=[IntentItem("enquiry", text=ctx.text)], in_scope=True)
            if self._wants_human(ctx) or self._wants_to_stop(ctx):  # a quiet "please stop" is confirmed for every agent
                return ctx
            if not ctx.verdict.in_scope or ctx.verdict.intent == "off_topic":
                ctx.verdict.in_scope = False
                st.digressions += 1
            else:
                st.digressions = 0
            log.info("verdict", intent=ctx.verdict.intent, in_scope=ctx.verdict.in_scope, digressions=st.digressions)
            return ctx
        if ctx.candidate_action and not ctx.text.strip():  # a bare button press: the action is the intent, nothing to classify
            ctx.verdict = AnswerVerdict("answered", intent="new_answer", reason="candidate action", skipped=True)
            ctx.trace.append(StageTrace("decide.intent_detector.llm", 0.0, skipped=True, note="candidate action"))
            return ctx
        if st.open_question is None and not st.turns:  # opening message: the greeting step runs regardless of what it says
            ctx.verdict = AnswerVerdict("answered", intent="new_answer", reason="first turn", skipped=True)
            if len(ctx.text.split()) <= 3:  # "hello", "hi there"
                ctx.trace.append(StageTrace("decide.intent_detector.llm", 0.0, skipped=True, note="first turn"))
                return ctx
            try:  # a fuller opening ("Yes, I consent. I live in Austin") is scanned so volunteered facts prefill later steps
                scan = await asyncio.wait_for(self._call(ctx), timeout=d.intent_timeout_s)
            except Exception as e:
                log.error("opening scan failed; nothing prefilled", error=f"{e.__class__.__name__}: {e}")
                return ctx
            for f, val in scan.fields.items():
                if f in known_fields(ctx) and f not in st.fields and str(val or "").strip():
                    st.prefilled[f] = val
            log.info("opening scan", prefilled=sorted(scan.fields))
            return ctx
        try:
            ctx.verdict = await asyncio.wait_for(self._call(ctx), timeout=d.intent_timeout_s)
        except Exception as e:  # fail safe: a classifier hiccup is never an answer, a digression or a step toward an exit
            log.error("intent detector failed; asking again without counting", error=f"{e.__class__.__name__}: {e}")
            ctx.errors.append(f"intent_detector: {e.__class__.__name__}")
            ctx.verdict = AnswerVerdict("digressed", intent="enquiry", reason=DETECTOR_ERROR, items=[IntentItem("enquiry", text=ctx.text)], skipped=True)
            ctx.route = ROUTE_REASK
            return ctx
        v, q = ctx.verdict, st.open_question
        if ctx.handoff_reason or self._wants_human(ctx):  # read only for the consent answer it may carry; nothing counted
            return ctx
        if self._wants_to_stop(ctx):
            return ctx
        if st.completed:
            log.info("verdict on a completed session", intents=[i.intent for i in v.items])
            return ctx
        log.info(
            "verdict",
            message=redact_pii(ctx.text),
            intents=[i.intent for i in v.items],
            on_step=[i.on_step for i in v.items],
            fields=sorted(v.fields),
            edited_field=v.edited_field,
            needs_multi_step=v.needs_multi_step,
            clear_outputs=v.clear_outputs,
            conflict=bool(v.conflict),
            step=q.step if q else None,
            field=q.field if q else None,
            reason=v.reason,
        )
        for f, val in v.fields.items():  # answers given before they were asked are kept for the step that will ask them
            if f in known_fields(ctx) and f not in st.fields and f != (q.field if q else None) and f not in open_fields(ctx) and str(val or "").strip():
                st.prefilled[f] = val
        if v.verdict == "answered":
            st.digressions = 0
            return ctx
        current = q.step if q else StepGraph(ctx.policy).next_step(st)
        if current and v.intent == "enquiry" and v.in_scope and REGISTRY.node(ctx.policy.step(current).node).cls.takes_questions:
            ctx.verdict = replace(v, verdict="answered", reason=f"question for {current}")  # the step's own input
            st.digressions = 0
            log.info("routing", decision="step", step=current, reason="enquiry on a step that takes questions")
            return ctx
        step_mode = ctx.policy.step(q.step).mode if q else ctx.policy.mode
        # counting first (law 5), then the route: a planner detour is still an on-step enquiry or a digression
        detour = v.intent == "enquiry" and v.needs_multi_step and step_mode == "adaptive"
        if v.intent == "enquiry" and v.on_step:  # about the current step itself: never a digression, but capped per step
            step = q.step if q else "_"
            n = st.on_step_enquiries.get(step, 0) + 1
            st.on_step_enquiries[step] = n
            ctx.reask = True
            if n > d.on_step_enquiries:
                ctx.route = ROUTE_REASK
                log.info("routing", decision="reask", reason=f"on-step enquiries={n} > limit; asking the user to settle the step")
                return ctx
            self._answer_route(ctx, v, detour, "on-step enquiry; not counted as a digression")
            return ctx
        if GREETING.fullmatch(ctx.text):  # "hello" mid-conversation: greet back and ask again, nothing counted
            ctx.verdict = replace(v, reason=GREETING_ONLY)
            ctx.reask = True
            ctx.route = ROUTE_REASK
            log.info("routing", decision="reask", reason="greeting; not counted as a digression")
            return ctx
        st.digressions += 1
        ctx.services.metrics.incr("digressions")
        if st.digressions >= d.digressions:
            if st.nudges < d.nudges:
                st.nudges += 1
                ctx.route = ROUTE_NUDGE
                log.info("routing", decision="nudge", reason=f"digressions={st.digressions} >= limit", nudges=st.nudges)
            else:
                ctx.route = "exit"
                ctx.exit_reason = "disengaged"
                log.info("routing", decision="exit", reason="digression limit after nudge", digressions=st.digressions, nudges=st.nudges)
            return ctx
        ctx.reask = True
        self._answer_route(ctx, v, detour, f"{v.intent}; digressions={st.digressions}")
        return ctx

    @staticmethod
    def _wants_to_stop(ctx: TurnContext) -> bool:
        """A quiet opt-out the model noticed: ask to confirm before ending (a model never ends a conversation)."""
        if ctx.verdict is None or not ctx.verdict.wants_to_stop or ctx.state.finished:
            return False
        ctx.state.data[CONFIRM_STOP_KEY] = True
        ctx.route = "confirm_stop"
        ctx.decide("count", "stop_check", reason="model read a wish to stop", ref="model")
        log.info("routing", decision="confirm_stop", reason="user may want to stop")
        ctx.short_circuit_with(CONFIRM_STOP.get(ctx.language, CONFIRM_STOP["en"]))
        return True

    @staticmethod
    def _wants_human(ctx: TurnContext) -> bool:
        """The user asked for a person: the turn goes to the handoff step, counted as nothing."""
        if ctx.verdict is None or not ctx.verdict.wants_human or ctx.state.finished:
            return False
        ctx.handoff_reason = "user_request"
        log.info("routing", decision="handoff", reason="user asks for a person")
        return True

    @staticmethod
    def _answer_route(ctx: TurnContext, v: AnswerVerdict, detour: bool, reason: str) -> None:
        """Where an enquiry is answered: the planner for a multi-step one in adaptive mode, else the knowledge base."""
        if detour:
            ctx.planner_detour = True
            ctx.route = ROUTE_PLANNER
            log.info("routing", decision="planner_detour", reason=f"needs_multi_step enquiry in adaptive mode; {reason}")
            return
        ctx.route = "knowledge" if (v.intent == "enquiry" and ctx.services.gateway.is_allowed("knowledge_search")) else ROUTE_REASK
        log.info("routing", decision=ctx.route, reason=reason)

from __future__ import annotations

import re
from typing import Any

from pydantic import Field

from yachat.capabilities.nodes.common import affirm, affirm_failed, answers_for, tool_failed
from yachat.registry.core import CapabilityConfig, Node, NodeResult, node
from yachat.runtime.context import TurnContext
from yachat.runtime.llm import LLMRequest

EXPECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"valid": {"type": "boolean"}, "normalized": {"type": "string"}, "reason": {"type": "string"}},
    "required": ["valid", "normalized", "reason"],
    "additionalProperties": False,
}
#: with `curious`: also what a valid answer leaves out, and one question for it
CURIOUS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {**EXPECT_SCHEMA["properties"], "missing": {"type": "string"}, "follow_up": {"type": "string"}},
    "required": ["valid", "normalized", "reason", "missing", "follow_up"],
    "additionalProperties": False,
}
CURIOUS = (
    " If valid, also judge whether a person handling it could act on it without asking. Only when the answer names a "
    'problem without saying what it is ("it\'s broken", "something\'s wrong", "doesn\'t work"): missing = what is '
    "unclear, in a few words, and follow_up = one short, friendly question asking for it. An answer that says what "
    'happened ("it never arrived", "wrong size sent", "the screen is cracked") is enough: leave both empty. Always '
    "leave them empty for ids, numbers, codes, names, dates, contact details and yes/no answers."
)
PII_HINTS = {"address": ("address", "dirección"), "email": ("email", "e-mail"), "phone": ("phone", "mobile", "telephone"), "ssn": ("ssn", "social security")}
CONFIRM = "__confirm"
#: the follow-up on a thin answer (`curious`); one per field, counted in code
MORE = "__more"
#: a reply to the follow-up that adds nothing
NOTHING_MORE = re.compile(r"\s*(?:no(?:pe)?|nothing(?: (?:else|more))?|that'?s (?:it|all)|not really|n/?a|-)[\s.!]*", re.I)
REVIEW_FIELD = "final_review"
#: the candidate's explicit instruction to send: a submit verb with at most a yes/please around it and "it" after it.
#: Questions ("how do I apply?") and anything else ("send me a copy") are not an instruction to submit.
SUBMIT = re.compile(
    r"(?:(?:yes|yep|yeah|ok|okay|sure|great|perfect|looks good|all good|go ahead|please|s[ií])[\s,.!]*)*"
    r"(?:submit|send|apply|enviar|env[ií]a(?:lo|la)?)"
    r"(?:\s+(?:it|this|that|now|please|my application|the application|ya|por favor))*[\s.!]*",
    re.I,
)


def says_submit(text: str) -> bool:
    return SUBMIT.fullmatch(text.strip()) is not None


class FieldSpec(CapabilityConfig):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    ask: str
    expect: str
    optional: bool = False
    max_reasks: int | None = Field(default=None, ge=1)


class FormFillConfig(CapabilityConfig):
    fields: list[FieldSpec] = Field(default_factory=list)
    #: show a final review and let the candidate submit (candidate-only action `submit_application`)
    review: bool = True
    #: after a valid but thin answer, ask one follow-up for the detail that matters (at most once per field)
    curious: bool = False
    #: said once every field is answered, e.g. what happens next ("We'll email you a return label within a day.")
    closing: str = ""


def pii_categories(f: FieldSpec) -> set[str]:
    hay = f"{f.name} {f.expect}".lower()
    return {cat for cat, words in PII_HINTS.items() if any(w in hay for w in words)}


def _label(name: str) -> str:
    return name.replace("_", " ")


@node("form_fill", produces=["form"])
class FormFill(Node[FormFillConfig]):
    """Asks one field at a time, validates against `expect`, confirms remembered or pre-given values, then a final review
    where only the candidate's explicit "submit" runs the submit action."""

    config_model = FormFillConfig

    async def _check(self, ctx: TurnContext, f: FieldSpec, value: str, curious: bool = False) -> tuple[bool, str, str, str]:
        """valid, normalized, reason, and (when `curious` and the field has not been followed up) a follow-up question."""
        curious = curious and f.name not in ctx.state.data.get("probes", {})
        system = (
            f"Decide whether the value satisfies the expectation for field '{f.name}'. Expectation: {f.expect}. "
            "Return valid=true with normalized = the value as the user wrote it, trimmed, with only obvious typo or casing fixes; "
            "otherwise valid=false and a short reason." + (CURIOUS if curious else "")
        )
        req = LLMRequest(
            "expect",
            ctx.services.models.classify,
            system,
            [{"role": "user", "content": value}],
            max_tokens=256,
            effort=ctx.services.models.effort_classify,
            metadata={"field": f.name, "value": value},
        )
        out = await ctx.services.llm.structured(req, CURIOUS_SCHEMA if curious else EXPECT_SCHEMA)
        ctx.llm_calls += 1
        follow = str(out.get("follow_up") or "").strip() if curious and str(out.get("missing") or "").strip() else ""
        return bool(out.get("valid")), str(out.get("normalized") or value), str(out.get("reason", "")), follow

    def _remembered(self, ctx: TurnContext, f: FieldSpec) -> tuple[Any, bool] | None:
        """(value, needs_confirmation) from memory, or None when absent/stale."""
        spec = ctx.policy.memory_item(f.name)
        if spec is None:
            return None
        rec = next((m for m in ctx.memory if m.item == f.name), None)
        if rec is None or rec.weight() < ctx.policy.defaults.stale_memory_threshold:
            return None
        return rec.value, spec.confirm_on_reuse

    def _ask(self, ctx: TurnContext, f: FieldSpec, prefix: str) -> NodeResult:
        st = ctx.state
        if f.name in st.prefilled:  # given earlier in the conversation, before it was asked
            val = st.prefilled.pop(f.name)
            st.data["prefill"] = {"field": f.name, "value": val}
            ask = f"You mentioned {val}. Should I use that as your {_label(f.name)}?"
            return NodeResult(
                prefix + ask,
                goal=f"Check whether to use {val}, which they mentioned, as their {_label(f.name)}.",
                ask=(f.name + CONFIRM, ask, "yes or no"),
                allowed_pii=pii_categories(f),
            )
        rem = self._remembered(ctx, f)
        if rem is not None:
            value, confirm = rem
            if not confirm:
                st.fields[f.name] = value
                return self._next(
                    ctx, prefix + f"Using your saved {_label(f.name)}.\n", pii_categories(f), f"Mention you're using their saved {_label(f.name)}."
                )
            st.data["prefill"] = {"field": f.name, "value": value}
            ask = f"Is your {_label(f.name)} still {value}?"
            return NodeResult(
                prefix + ask,
                goal=f"Check their saved {_label(f.name)} is still right.",
                ask=(f.name + CONFIRM, ask, "yes or no"),
                allowed_pii=pii_categories(f),
            )
        return NodeResult(prefix + f.ask, goal="Ask the next question.", ask=(f.name, f.ask, f.expect), allowed_pii=pii_categories(f))

    async def _fail(self, ctx: TurnContext, f: FieldSpec, reason: str) -> NodeResult | None:
        n = ctx.state.reasks.get(f.name, 0) + 1
        ctx.state.reasks[f.name] = n
        limit = f.max_reasks or ctx.policy.defaults.reasks
        if n >= limit:
            if not f.optional:
                return NodeResult(
                    f"I wasn't able to get a valid {_label(f.name)}.",
                    goal=f"Say you couldn't get a valid {_label(f.name)}, so you'll stop here.",
                    exit_reason="max_reasks",
                )
            ctx.state.fields[f.name] = None
            return None
        text = f"I didn't quite get that{(' (' + reason + ')') if reason else ''}. {f.ask}"
        goal = f"Their {_label(f.name)} didn't look right" + (f" ({reason})" if reason else "") + ": say so kindly and ask again."
        return NodeResult(text, goal=goal, ask=(f.name, f.ask, f.expect), allowed_pii=pii_categories(f))

    def _next(self, ctx: TurnContext, prefix: str, allowed: set[str], goal: str = "") -> NodeResult:
        """The next question (or the review), with `goal` (what happened this turn) ahead of the question's own."""
        cfg = self._cfg
        r = next((self._ask(ctx, f, prefix) for f in cfg.fields if f.name not in ctx.state.fields), None) or self._review(ctx, prefix, allowed)
        r.allowed_pii |= allowed
        r.goal = " ".join(g for g in (goal, r.goal) if g)
        return r

    def _review(self, ctx: TurnContext, prefix: str, allowed: set[str]) -> NodeResult:
        st = ctx.state
        job = st.outputs.get("selected_job") or {}
        can_submit = self._cfg.review and ctx.services.actions is not None and bool(job)
        if not can_submit:
            closing = self._cfg.closing.strip()
            return NodeResult(
                prefix + (closing or "That's everything I need."),
                goal=f"Say you have everything you need, then say what happens next, as given: {closing}" if closing else "Say you have everything you need.",
                outputs={"form": dict(st.fields)},
                complete=True,
                allowed_pii=allowed,
            )
        lines = "\n".join(f"- {_label(k)}: {v}" for k, v in st.fields.items() if v is not None)
        ask = f"Here is your application for {job.get('title')}:\n{lines}\nReply 'submit' to send it, or tell me what to change."
        return NodeResult(
            prefix + ask,
            goal="Show the application summary exactly as listed and ask them to reply 'submit' or say what to change.",
            ask=(REVIEW_FIELD, ask, "'submit', or a correction"),
            allowed_pii=allowed | {"address", "email", "phone"},
        )

    async def act(self, ctx: TurnContext, config: FormFillConfig) -> NodeResult:
        self._cfg = config
        by_name = {f.name: f for f in config.fields}
        st = ctx.state
        q = st.open_question
        answers = {k: v for k, v in answers_for(ctx, "form_fill").items() if str(v).strip()}  # a bare button click carries no answer
        prefix, goal = "", ""
        allowed: set[str] = set()

        if (
            q
            and q.field == REVIEW_FIELD
            and (ctx.candidate_action == "submit_application" or says_submit(ctx.text))  # the user's own words, not a normalized answer
        ):
            ctx.candidate_action = "submit_application"  # the candidate's own explicit submit
            return await self._submit(ctx)
        if answers and q and q.field == REVIEW_FIELD:
            value = next(iter(answers.values()))
            if await affirm(ctx, value, q.ask) == "no":
                return NodeResult(
                    "Okay, I won't submit it. Your answers are saved.",
                    goal="Confirm you won't submit and their answers are saved.",
                    outputs={"form": dict(st.fields)},
                    complete=True,
                )
            ask = "Tell me what to change, or reply 'submit' to send it."
            return NodeResult(ask, goal="Ask what to change, or to reply 'submit' to send it.", ask=(REVIEW_FIELD, ask, "'submit', or a correction"))

        if answers and q and q.field.endswith(MORE):  # the reply to a follow-up
            fname = q.field[: -len(MORE)]
            st.open_question = None
            if set(answers) <= {q.field, fname}:  # more about the same thing: added to the field's answer
                more = next(iter(answers.values())).strip()
                if more and not NOTHING_MORE.fullmatch(more):  # "No power at all" is a detail; only a bare "no" adds nothing
                    st.fields[fname] = f"{st.fields.get(fname) or ''}. {more}".strip(". ")
                    ctx.validated_answers[fname] = st.fields[fname]
                ctx.decide("answer", "followed_up", ref=fname)
                return self._next(ctx, "Thanks.\n", allowed, "Thank them for the detail in a few words, without repeating it back.")
            # they answered something else instead: the follow-up is dropped and the answer handled as usual

        if answers and q and q.field.endswith(CONFIRM):
            fname = q.field[: -len(CONFIRM)]
            value = next(iter(answers.values()))
            pre = st.data.pop("prefill", {})
            meant = await affirm(ctx, value, q.ask)
            if meant == "yes":
                st.fields[fname] = pre.get("value")
                ctx.validated_answers[fname] = pre.get("value")
                prefix = "Great.\n"
                answers = {}
                goal = "Thank them for confirming."
            elif meant == "no":
                ctx.memory = [m for m in ctx.memory if m.item != fname]
                f = by_name[fname]
                return NodeResult(
                    f"No problem. {f.ask}", goal="Say no problem, and ask the question.", ask=(fname, f.ask, f.expect), allowed_pii=pii_categories(f)
                )
            elif affirm_failed(ctx):  # could not read the reply: keep the remembered value, ask again, not counted
                st.data["prefill"] = pre
                return NodeResult(f"Sorry, I didn't catch that. {q.ask}", goal="Say you didn't catch that, and ask again.", ask=(q.field, q.ask, q.expect))
            else:
                answers = {fname: value}

        if answers:
            got: list[str] = []
            for fname, value in answers.items():
                spec = by_name.get(fname)
                if spec is None or fname in st.fields:
                    continue
                # curious only about the question just asked: an answer given ahead of its question is never followed up
                asked = q is not None and q.field == fname and len(answers) == 1
                valid, normalized, reason, follow = await self._check(ctx, spec, value, config.curious and asked)
                if valid:
                    st.fields[fname] = normalized
                    ctx.validated_answers[fname] = normalized
                    st.reasks.pop(fname, None)
                    allowed |= pii_categories(spec)
                    got.append(f"{_label(fname)}: {normalized}")
                    if follow:  # thin but valid: kept, and one question for what it leaves out (never asked twice)
                        st.data.setdefault("probes", {})[fname] = 1
                        ctx.decide("answer", "probed", reason="thin answer", ref=fname)
                        goal = f"Acknowledge their {_label(fname)} in a few words, then ask this follow-up in your own words: {follow}"
                        more_q = (fname + MORE, follow, "more detail, or that there is nothing to add")
                        return NodeResult(follow, goal=goal, ask=more_q, free_text=True, allowed_pii=allowed)
                else:
                    r = await self._fail(ctx, spec, reason)
                    if r is not None:
                        return r
                    got.append(f"{_label(fname)}: skipped")
            if got:
                prefix = "Thanks.\n"  # the values are read back in the final review
                goal = "Acknowledge their answer in a few words, without repeating it back."
        return self._next(ctx, prefix, allowed, goal)

    async def _submit(self, ctx: TurnContext) -> NodeResult:
        st = ctx.state
        job = st.outputs.get("selected_job") or {}
        answers = {k: str(v) for k, v in st.fields.items() if v is not None}
        assert ctx.services.actions is not None
        res = await ctx.services.actions.run("submit_application", {"job_id": job.get("id", ""), "answers": answers}, ctx.candidate_action)
        if not res.ok:
            tool_failed(ctx, "submit_application", res.error_kind)
            return NodeResult(
                "I couldn't submit right now; your answers are saved and we'll retry.",
                goal="Say sending failed for now; their answers are saved and it will be retried.",
                outputs={"form": dict(st.fields)},
                complete=True,
            )
        rid = (res.data or {}).get("application_id", "")
        return NodeResult(
            f"Submitted! Your application id is {rid}.",
            goal="Confirm the application was sent and give its id.",
            outputs={"form": dict(st.fields), "application": res.data},
            complete=True,
        )

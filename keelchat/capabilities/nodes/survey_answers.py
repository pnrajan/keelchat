"""Survey questions and how a typed reply becomes the value a closed question asked for. Ported from Verlox
(app/config_schema/schema.py QuestionSpec, app/workflow/answers.py).

Closed questions are measurements: a 1-10 rating, a top three, 100 points split across factors. Stored as prose they
can't be counted. Parsing is generous about form ("probably a 7", "b and d") and never guesses: a reply that could mean
two things is answered with a question. Every message here is written for the person answering."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from keelchat.registry.core import CapabilityConfig


class QuestionKind(StrEnum):
    open = "open"
    single_choice = "single_choice"
    multi_choice = "multi_choice"
    scale = "scale"
    rank = "rank"
    allocation = "allocation"
    number = "number"


CLOSED_KINDS = frozenset(k for k in QuestionKind if k is not QuestionKind.open)
_NEEDS_OPTIONS = frozenset({QuestionKind.single_choice, QuestionKind.multi_choice, QuestionKind.rank, QuestionKind.allocation})
#: a leading list marker on a pasted option: "(a) ", "b. ", "1) "
_ENUMERATOR = re.compile(r"^\s*[\(\[]?[a-zA-Z0-9][\)\].]\s+")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _ENUMERATOR.sub("", text).lower()).strip("_")[:60] or "option"


class ChoiceOption(CapabilityConfig):
    """`label` is what people read; `key` is what the answer is stored as. A bare string in YAML is both."""

    key: str = Field(min_length=1, max_length=60)
    label: str = Field(min_length=1, max_length=300)

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"key": _slug(value), "label": _ENUMERATOR.sub("", value).strip() or value}
        return value


class ConditionOp(StrEnum):
    """How an `ask_if` condition reads an earlier answer; always decided in code."""

    answered = "answered"
    equals = "equals"
    any_of = "any_of"
    gte = "gte"
    lte = "lte"


class AskIf(CapabilityConfig):
    """Ask only when an earlier question's answer meets the condition. Unanswered → the condition is false."""

    question: str = Field(min_length=1, max_length=60)
    op: ConditionOp = ConditionOp.equals
    value: str = Field(default="", max_length=300)
    values: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _operand(self) -> AskIf:
        if self.op is ConditionOp.any_of and not self.values:
            raise ValueError("an any_of condition needs values")
        if self.op is ConditionOp.equals and not self.value:
            raise ValueError("an equals condition needs a value")
        if self.op in (ConditionOp.gte, ConditionOp.lte):
            try:
                float(self.value)
            except ValueError as e:
                raise ValueError(f"a {self.op.value} condition needs a number") from e
        return self


#: which conditions can read which kind of answer
_OPS_BY_KIND = {
    QuestionKind.open: {ConditionOp.answered},
    QuestionKind.single_choice: {ConditionOp.answered, ConditionOp.equals, ConditionOp.any_of},
    QuestionKind.multi_choice: {ConditionOp.answered, ConditionOp.equals, ConditionOp.any_of},
    QuestionKind.scale: {ConditionOp.answered, ConditionOp.equals, ConditionOp.gte, ConditionOp.lte},
    QuestionKind.number: {ConditionOp.answered, ConditionOp.equals, ConditionOp.gte, ConditionOp.lte},
    QuestionKind.rank: {ConditionOp.answered},
    QuestionKind.allocation: {ConditionOp.answered},
}


class SurveyQuestion(CapabilityConfig):
    """One question. Open questions are conversational: `expectation` names what a complete answer covers and the agent
    asks up to `max_probes` follow-ups until it does. Closed kinds capture a value and are never probed; `ask_why` adds
    one open "why?" after the value."""

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=60)
    prompt: str = Field(min_length=1, max_length=1000)
    kind: QuestionKind = QuestionKind.open
    expectation: str = Field(default="", max_length=600)
    max_probes: int = Field(default=2, ge=0, le=5)
    ask_why: bool = False
    optional: bool = False
    ask_if: AskIf | None = None
    options: list[ChoiceOption] = Field(default_factory=list, max_length=26)
    max_select: int = Field(default=0, ge=0, le=26)
    top_n: int = Field(default=3, ge=1, le=26)
    scale_min: int = Field(default=1, ge=0, le=100)
    scale_max: int = Field(default=10, ge=1, le=100)
    min_label: str = Field(default="", max_length=60)
    max_label: str = Field(default="", max_length=60)
    total_points: int = Field(default=100, ge=1, le=1000)
    unit: str = Field(default="", max_length=30)
    min_value: float | None = None
    max_value: float | None = None

    @field_validator("options")
    @classmethod
    def _unique_keys(cls, options: list[ChoiceOption]) -> list[ChoiceOption]:
        keys = [o.key for o in options]
        if len(set(keys)) != len(keys):
            raise ValueError("two options share a key")
        return options

    @model_validator(mode="after")
    def _shape(self) -> SurveyQuestion:
        k = self.kind
        if k in _NEEDS_OPTIONS and len(self.options) < 2:
            raise ValueError(f"a {k.value} question needs at least two options")
        if k not in _NEEDS_OPTIONS and self.options:
            raise ValueError(f"a {k.value} question takes no options")
        if k is QuestionKind.rank and self.top_n > len(self.options):
            raise ValueError("top_n is larger than the number of options")
        if k is QuestionKind.multi_choice and self.max_select > len(self.options):
            raise ValueError("max_select is larger than the number of options")
        if k is QuestionKind.scale and self.scale_min >= self.scale_max:
            raise ValueError("scale_min must be below scale_max")
        if k is QuestionKind.number and self.min_value is not None and self.max_value is not None and self.min_value > self.max_value:
            raise ValueError("min_value is above max_value")
        if k in CLOSED_KINDS and self.expectation:
            raise ValueError(f"a {k.value} question can't carry an expectation: closed answers are read, not probed")
        if self.ask_why and k not in CLOSED_KINDS:
            raise ValueError("ask_why belongs on a closed question; open answers are already probed")
        return self

    @property
    def closed(self) -> bool:
        return self.kind in CLOSED_KINDS


def check_conditions(questions: list[SurveyQuestion]) -> None:
    """Every `ask_if` points at an earlier question, with an operator its kind supports and options it offers."""
    seen: dict[str, SurveyQuestion] = {}
    for q in questions:
        if q.key in seen:
            raise ValueError(f"two questions share the key {q.key!r}")
        c = q.ask_if
        if c is not None:
            target = seen.get(c.question)
            if target is None:
                raise ValueError(f"question {q.key!r}: ask_if names {c.question!r}, which is not an earlier question")
            if c.op not in _OPS_BY_KIND[target.kind]:
                raise ValueError(f"question {q.key!r}: a {c.op.value} condition can't read a {target.kind.value} answer")
            if target.options and c.op in (ConditionOp.equals, ConditionOp.any_of):
                unknown = ({c.value} | set(c.values)) - {o.key for o in target.options} - {""}
                if unknown:
                    raise ValueError(f"question {q.key!r}: {c.question!r} has no option {', '.join(sorted(unknown))}")
        seen[q.key] = q


def condition_met(c: AskIf, value: Any) -> bool:
    """Code decides; an unanswered or skipped question never meets a condition."""
    if value is None or value == "" or value == [] or value == {}:
        return False
    if c.op is ConditionOp.answered:
        return True
    if c.op in (ConditionOp.equals, ConditionOp.any_of):
        got = set(value) if isinstance(value, list) else {str(value)}
        want = {c.value} if c.op is ConditionOp.equals else set(c.values)
        return bool(got & want)
    try:
        n = float(value)
    except (TypeError, ValueError):
        return False
    return n >= float(c.value) if c.op is ConditionOp.gte else n <= float(c.value)


# --------------------------------------------------------------------------- reading replies

#: a reply's parts: commas, semicolons, slashes, "and", "then", arrows
_SEPARATORS = re.compile(r"\s*(?:,|;|/|\||->|>|\band\b|\bthen\b)\s*", re.IGNORECASE)


class Capture(StrEnum):
    """How a closed value was arrived at: read from the reply, or the model's reading the person then confirmed."""

    parsed = "parsed"
    confirmed = "confirmed"
    #: a score clicked in the invite email
    clicked = "clicked"


class Parsed(BaseModel):
    """A value (the question's shape: str, list, dict or number), or a message for the person."""

    value: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.value is not None


def letter(index: int) -> str:
    return chr(ord("a") + index) if index < 26 else str(index + 1)


def instruction(q: SurveyQuestion) -> str:
    """How to answer, said with the question (a person reading one message at a time has nowhere else to look)."""
    if q.kind is QuestionKind.scale:
        ends = f" ({q.scale_min} = {q.min_label}, {q.scale_max} = {q.max_label})" if q.min_label or q.max_label else ""
        return f"Answer with a number from {q.scale_min} to {q.scale_max}{ends}."
    if q.kind is QuestionKind.single_choice:
        return "Pick one."
    if q.kind is QuestionKind.multi_choice:
        return f"Pick as many as apply{f' (up to {q.max_select})' if q.max_select else ''}."
    if q.kind is QuestionKind.rank:
        return f"List your top {q.top_n}, best first."
    if q.kind is QuestionKind.allocation:
        return f"Split {q.total_points} points across these; they should add up to {q.total_points}."
    if q.kind is QuestionKind.number:
        return f"A number is fine{f' in {q.unit}' if q.unit else ''}; a rough figure is better than none."
    return ""


def parse(q: SurveyQuestion, raw: str) -> Parsed:
    text = raw.strip()
    if not text:
        return Parsed(error="Sorry, I didn't catch that. Could you try again?")
    return {
        QuestionKind.scale: _scale,
        QuestionKind.number: _number,
        QuestionKind.single_choice: _single,
        QuestionKind.multi_choice: _multi,
        QuestionKind.rank: _rank,
        QuestionKind.allocation: _allocation,
    }.get(q.kind, lambda q, t: Parsed(value=t))(q, text)


def _scale(q: SurveyQuestion, text: str) -> Parsed:
    figure = _first_number(text)
    if figure is None:
        return Parsed(error=f"A number from {q.scale_min} to {q.scale_max} is all I need here.")
    v = int(round(figure))
    if not q.scale_min <= v <= q.scale_max:
        return Parsed(error=f"That's outside the scale; anywhere from {q.scale_min} to {q.scale_max} works.")
    return Parsed(value=v)


def _number(q: SurveyQuestion, text: str) -> Parsed:
    figure = _first_number(text)
    if figure is None:
        return Parsed(error=f"Could you give me a rough number{f' ({q.unit})' if q.unit else ''}?")
    if q.min_value is not None and figure < q.min_value:
        return Parsed(error=f"That looks low; I'd expect at least {_plain(q.min_value)}.")
    if q.max_value is not None and figure > q.max_value:
        return Parsed(error=f"That looks high; I'd expect at most {_plain(q.max_value)}.")
    return Parsed(value=figure)


def _single(q: SurveyQuestion, text: str) -> Parsed:
    m = _match(q.options, text)
    if not m:
        return Parsed(error="I didn't catch which one; the letter on its own is fine.")
    if len(m) > 1:
        return Parsed(error=f"Just to be sure, did you mean {' or '.join(q.options[i].label for i in m[:3])}?")
    return Parsed(value=q.options[m[0]].key)


def _multi(q: SurveyQuestion, text: str) -> Parsed:
    picked: list[int] = []
    for part in _split(text):
        m = _match(q.options, part)
        if len(m) == 1 and m[0] not in picked:
            picked.append(m[0])
    if not picked:
        return Parsed(error="I didn't catch any of those; the letters are fine, like 'a, c'.")
    if q.max_select and len(picked) > q.max_select:
        return Parsed(error=f"That's more than {q.max_select}; which would you keep?")
    return Parsed(value=[q.options[i].key for i in picked])


def _rank(q: SurveyQuestion, text: str) -> Parsed:
    ordered: list[int] = []
    for part in _split(text):
        m = _match(q.options, part)
        if len(m) == 1 and m[0] not in ordered:
            ordered.append(m[0])
    if len(ordered) < q.top_n:
        return Parsed(error=f"I caught {len(ordered)}; could you give me your top {q.top_n}, best first?")
    return Parsed(value=[q.options[i].key for i in ordered[: q.top_n]])


def _allocation(q: SurveyQuestion, text: str) -> Parsed:
    alloc: dict[str, int] = {}
    for part in _split(text):
        figure = _first_number(part)
        if figure is None:
            continue
        m = _match(q.options, re.sub(r"\d+(?:\.\d+)?", " ", part))  # the number can't be read as a letter or position
        if len(m) == 1:
            alloc[q.options[m[0]].key] = int(round(figure))
    if not alloc:
        return Parsed(error=f"Could you split {q.total_points} points across them, something like 'a 50, b 30, c 20'?")
    total = sum(alloc.values())
    if total != q.total_points:
        return Parsed(error=f"That adds up to {total}; could you adjust it to {q.total_points}?")
    return Parsed(value=alloc)


def _split(text: str) -> list[str]:
    return [p for p in _SEPARATORS.split(text) if p.strip()]


def _match(options: list[ChoiceOption], text: str) -> list[int]:
    """Which options a fragment could mean, strongest signal first: a letter or number alone, an exact key or label,
    then wording (fragments under 3 characters never match by wording)."""
    cleaned = text.strip().lower().strip("().:")
    if not cleaned:
        return []
    if len(cleaned) == 1:
        for i in range(len(options)):
            if cleaned == letter(i):
                return [i]
    if cleaned.isdigit():
        pos = int(cleaned) - 1
        if 0 <= pos < len(options):
            return [pos]
    exact = [i for i, o in enumerate(options) if cleaned in (o.key, o.label.strip().lower())]
    if exact:
        return exact
    if len(cleaned) >= 3:
        return [i for i, o in enumerate(options) if cleaned in o.label.lower() or o.label.lower() in cleaned]
    return []


def _first_number(text: str) -> float | None:
    """The first figure, ignoring ordinals ("1st"); a hyphen inside "5-10" is a range, not a minus."""
    for m in re.finditer(r"(?<![\w.])(-?\d+(?:\.\d+)?)(\s*(?:st|nd|rd|th)\b)?", text.lower()):
        if not m.group(2):
            return float(m.group(1))
    return None


def _plain(figure: float) -> str:
    return str(int(figure)) if figure == int(figure) else str(figure)


def describe(q: SurveyQuestion, value: Any) -> str:
    """A captured value said back in words, so the person can confirm or correct it."""
    labels = {o.key: o.label for o in q.options}
    if q.kind is QuestionKind.scale:
        return f"{value} out of {q.scale_max}"
    if q.kind is QuestionKind.number:
        return f"{_plain(float(value))}{f' {q.unit}' if q.unit else ''}"
    if q.kind is QuestionKind.single_choice:
        return labels.get(str(value), str(value))
    if q.kind is QuestionKind.multi_choice:
        return ", ".join(labels.get(k, k) for k in value)
    if q.kind is QuestionKind.rank:
        return ", then ".join(labels.get(k, k) for k in value)
    if q.kind is QuestionKind.allocation:
        return ", ".join(f"{labels.get(k, k)} {v}" for k, v in value.items())
    return str(value)


def canonical_form(q: SurveyQuestion) -> str:
    """The exact reply shape the model must produce when it normalises a messy reply."""
    return {
        QuestionKind.scale: f"a single whole number from {q.scale_min} to {q.scale_max}",
        QuestionKind.number: "a single number",
        QuestionKind.single_choice: "exactly one option letter, like: b",
        QuestionKind.multi_choice: "the chosen option letters separated by commas, like: a, c",
        QuestionKind.rank: f"exactly {q.top_n} option letters, best first, like: b, a, d",
        QuestionKind.allocation: f"each option letter with its points, like: a 50, b 30, c 20 (adding to {q.total_points})",
    }.get(q.kind, "")


#: steps that ask survey questions, and how to read their questions from a step's settings
def survey_questions_of(steps: Any) -> list[SurveyQuestion]:
    """Every survey question of a compiled policy's steps: the classic survey's questions and a conversation survey's
    objectives, so results, NPS and exports read both the same way."""
    from keelchat.capabilities.nodes.conversation_survey import STEP as CONVERSATION
    from keelchat.capabilities.nodes.conversation_survey import ConversationSurveyConfig
    from keelchat.capabilities.nodes.survey import STEP as CLASSIC
    from keelchat.capabilities.nodes.survey import SurveyConfig

    out: list[SurveyQuestion] = []
    for s in steps:
        if s.node == CLASSIC:
            out += SurveyConfig.model_validate(dict(s.settings)).questions
        elif s.node == CONVERSATION:
            out += ConversationSurveyConfig.model_validate(dict(s.settings)).questions()
    return out

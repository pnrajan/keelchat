"""Draft a policy from a plain description: the model writes YAML from the registry's catalog, the real build checks
it, and its errors (YAML paths) are fed back. Attempts are counted in code; the draft is never registered or saved."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from keelchat.datasets.models import DatasetKind, field_dataset_kind
from keelchat.logging import get_logger
from keelchat.policy.compiler import build
from keelchat.policy.errors import PolicyError, PolicyValidationError
from keelchat.registry.core import REGISTRY
from keelchat.runtime.llm import LLMClient, LLMRequest, ModelConfig, untrusted

log = get_logger("drafter")

#: the first draft plus repairs from the build's errors
DRAFT_ATTEMPTS = 3

SYSTEM = """You write agent policies: one YAML file that defines a goal-oriented chatbot. Reply with the YAML only.
Keys (no others): agent, charter, languages, tools, flow, guardrails, limits, memory, mode, rules, handoff, small_talk.
- agent: lowercase id with dashes. charter: 2-4 sentences: what the bot helps with, its tone, and what is out of scope.
- flow: the steps in order, each a step name from the catalog, or `- name: {settings}` with that step's settings.
  Use only steps, tools, guardrails and datasets named in the catalog. A step that needs a dataset must name one of
  the listed datasets of its kind; if none fits, leave that step out.
- Questions answered from documents: add `knowledge_search` to `tools` (answers side questions during any step), or
  a `knowledge` step when answering them is the bot's main job.
- Settings follow each step's `format` (a JSON Schema) exactly. Quote strings YAML would read as true/false/null
  ("yes", "no", "on", "off").
- For surveys and feedback, prefer the `conversation_survey` step: write what the owner wants to learn as objectives
  (`learn`), not fixed questions; use `survey` only when the owner asks for exact questions in a fixed order.
- A "how likely are you to recommend us" question or objective has key `nps` (an objective with `measure: nps`; a
  question as a 0-10 scale); that is what gets an NPS score.
- Add `memory` only when the description asks to remember something about people between conversations; a survey
  needs none.
- rules: only for situations the description names: `- {name: snake_case, when: the situation in the user's words,
  then: what the reply must do}`.
- handoff: when the description wants a person reachable: `handoff: {when: [user_request]}` (also max_reasks).
  When it says to hand over in a situation ("emergencies go to a person", "refunds over $100 to a manager"), add a
  rule with `handoff: true` (the runtime passes the chat on; `then` only says what to tell them, e.g. "say it sounds
  urgent and a person will take over now"), plus the top-level `handoff`. Never write "I'll connect you" into a `then`
  without `handoff: true`. Never list `handoff` or `exit` as flow steps: the runtime runs them.
- small_talk: brief when the description asks for a friendly or chatty bot; omit otherwise.
- A step may take `think: true` (plan replies first; for nuanced or sensitive steps) and a form step `curious: true`
  (one follow-up on a vague answer, e.g. a problem description); add them only when the description asks for care.
- Keep it short: only what the description asks for. Omit keys whose defaults are fine.
Example:
agent: support-assistant
charter: >
  Help customers with orders and returns. Friendly, concise. Out of scope: legal advice.
flow:
  - greet_consent
  - form_fill:
      fields:
        - name: order_number
          ask: What is your order number?
          expect: An order number such as ORD-12345
guardrails: {input: [prompt_injection], output: [pii]}
limits: {reasks: 3, digressions: 3}
rules:
  - name: injury
    when: the customer says someone was hurt by a product
    then: say you're sorry and that a person will take over now
    handoff: true
handoff: {when: [user_request]}
memory:
  - field: preferred_contact
    extract: How they prefer to be contacted
    half_life: 90d"""


@dataclass
class Draft:
    yaml: str
    errors: list[PolicyError]
    attempts: int


def catalog(tenant: str, datasets: Mapping[str, DatasetKind]) -> str:
    """What a policy may name, from the registry: steps with their settings, tools, guardrails, datasets."""
    REGISTRY.discover()
    steps = []
    for name in REGISTRY.node_names():
        spec = REGISTRY.node(name)
        settings = {}
        for fname, info in spec.config_model.model_fields.items():
            note = "required" if info.is_required() else "optional"
            if kind := field_dataset_kind(info):
                names = [n for n, k in datasets.items() if k == kind]
                note += f"; a {kind} dataset name, one of {names}" + ("" if info.is_required() else "; omit it to use the shared knowledge base")
            settings[fname] = note
        # the full settings format (nested lists, conditions), so a draft gets shapes right the first time
        steps.append({"step": name, "does": spec.description.split("\n")[0], "settings": settings, "format": spec.config_model.model_json_schema()})
    body = {
        "steps": steps,
        "tools": REGISTRY.tool_names(tenant),
        "guardrails": {n: REGISTRY.guardrail(n).stage for n in REGISTRY.guardrail_names()},
        "datasets": dict(datasets),
    }
    return json.dumps(body, indent=1)


#: where a policy's YAML starts: a comment or a top-level key
_YAML_START = re.compile(r"^(#|[a-z_]+:)", re.MULTILINE)


def _yaml_only(text: str) -> str:
    """The YAML in a reply: inside a fence if there is one, else from the first comment or top-level key (a model may
    say something first)."""
    fenced = re.search(r"```(?:ya?ml)?\n(.*?)```", text, re.DOTALL)
    if fenced:
        return fenced.group(1).strip() + "\n"
    start = _YAML_START.search(text)
    return text[start.start() if start else 0 :].strip() + "\n"


async def draft_policy(llm: LLMClient, models: ModelConfig, description: str, tenant: str, datasets: Mapping[str, DatasetKind]) -> Draft:
    messages = [{"role": "user", "content": f"Catalog:\n{catalog(tenant, datasets)}\n\n" + untrusted("description", description)}]
    return await _until_it_builds(llm, models, "policy_draft", SYSTEM, messages, tenant, datasets)


CLARIFY = """You help someone describe a customer-facing chatbot so that its policy (a YAML file) can be written well.
The capabilities below are what a policy can express. For THIS description, work out which details the policy will need
that the description leaves open, and which of them would change the bot most. Ask about those only: 2-5 questions,
most important first, or none when nothing important is open. Nothing is a required topic; skip what the description
already settles or what doesn't matter for this bot.
For each question: `ask` in the person's own terms; `why`, one line of guidance on what the answer decides or what to
weigh; 2-5 `options` that make sense for this particular business (not generic placeholders), each with the plain
sentence it `adds` to the description; `multi` when several can apply together.
Also `understood`: one or two sentences on what you take the bot to be for, so the person can correct you.
Nothing shown to the person uses policy or YAML words. Never ask for names of people, keys, URLs or private data."""


def capabilities(tenant: str = "default") -> str:
    """A short view of what a policy can express (keys, steps, tools, guardrails), for questions before drafting."""
    REGISTRY.discover()
    return json.dumps(
        {
            "policy keys": "agent, charter (purpose, tone, out of scope), languages, flow (steps), tools, rules (when X then Y; "
            "may hand to a person), handoff (when a person takes over), guardrails, limits (re-asks, detours), memory "
            "(what to remember about a person, with consent), small_talk",
            "steps": {n: REGISTRY.node(n).description.split("\n")[0] for n in REGISTRY.node_names()},
            "tools": REGISTRY.tool_names(tenant),
            "guardrails": REGISTRY.guardrail_names(),
        },
        indent=1,
    )


#: questions shown at most, and options per question
CLARIFY_MAX, OPTIONS_MAX = 5, 5

CLARIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "understood": {"type": "string", "description": "one or two sentences: what the bot is for, as you understand it"},
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "short lowercase id"},
                    "ask": {"type": "string", "description": "the question, in plain words"},
                    "why": {"type": "string", "description": "one line of guidance: what the answer decides, or what to weigh"},
                    "multi": {"type": "boolean"},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "description": "2-6 words"},
                                "adds": {"type": "string", "description": "the sentence this choice adds to the description"},
                            },
                            "required": ["label", "adds"],
                        },
                    },
                },
                "required": ["id", "ask", "options"],
            },
        },
    },
    "required": ["understood", "questions"],
}


@dataclass(frozen=True)
class Option:
    label: str
    #: the sentence this choice adds to the description
    adds: str


@dataclass(frozen=True)
class Question:
    id: str
    ask: str
    why: str
    multi: bool
    options: list[Option]


@dataclass(frozen=True)
class Clarification:
    #: what the model takes the bot to be for, for the person to correct
    understood: str
    questions: list[Question]


def _clean(text: object, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


async def clarify(llm: LLMClient, models: ModelConfig, description: str, tenant: str = "default") -> Clarification:
    """What the model understood, and the questions it judges worth asking before drafting (none are fixed: it reads
    the description against what a policy can express), each with options to click. Checked in code: unique ids, 2 to
    OPTIONS_MAX options each, at most CLARIFY_MAX questions; anything malformed is dropped."""
    content = f"Capabilities:\n{capabilities(tenant)}\n\n" + untrusted("description", description)
    req = LLMRequest("policy_clarify", models.generate, CLARIFY, [{"role": "user", "content": content}], max_tokens=2500, effort="medium")
    out = await llm.structured(req, CLARIFY_SCHEMA)
    questions: list[Question] = []
    for raw in out.get("questions") or []:
        if not isinstance(raw, dict):
            continue
        qid = re.sub(r"[^a-z0-9_]+", "_", _clean(raw.get("id"), 40).lower()).strip("_") or f"q{len(questions) + 1}"
        options = [
            Option(_clean(o.get("label"), 60), _clean(o.get("adds"), 300))
            for o in (raw.get("options") or [])
            if isinstance(o, dict) and _clean(o.get("label"), 60) and _clean(o.get("adds"), 300)
        ][:OPTIONS_MAX]
        ask = _clean(raw.get("ask"), 200)
        if not ask or len(options) < 2 or any(q.id == qid for q in questions):
            continue
        questions.append(Question(qid, ask, _clean(raw.get("why"), 200), bool(raw.get("multi")), options))
        if len(questions) == CLARIFY_MAX:
            break
    log.info("questions before drafting", count=len(questions))
    return Clarification(_clean(out.get("understood"), 400), questions)


def with_answers(description: str, answers: Mapping[str, list[str]], questions: list[Question]) -> str:
    """The description plus the sentence each chosen option adds, in question order. Unknown ids and labels are ignored
    (they come from a browser); an answer may also be the person's own words ("other")."""
    lines = []
    for q in questions:
        picked = answers.get(q.id) or []
        for label in picked[: len(q.options) if q.multi else 1]:
            match = next((o for o in q.options if o.label == label), None)
            lines.append(match.adds if match else f"{q.ask} {_clean(label, 300)}")
    return description.strip() + ("\n\nDetails:\n" + "\n".join(f"- {line}" for line in lines) if lines else "")


EDIT = """You change an agent policy (YAML) as the owner asks. Reply with the whole changed YAML only.
- The reply is the policy file itself: top-level keys, never nested under another key.
- Do exactly what is asked and nothing else: keep every other key, step, field, rule and comment as written, and the
  same `agent` unless asked to rename it.
- Use only steps, tools, guardrails and datasets in the catalog; settings follow each step's `format`.
- Keys you may use: agent, charter, languages, tools, flow, guardrails, limits, memory, mode, rules, handoff, small_talk.
  A rule is `{name: snake_case, when: the situation in the user's words, then: what the reply must do}` (plus
  `handoff: true` to pass the chat to a person when it matches); a step may
  take `think`, `model` (small|large), `high_stakes` and `rules`; a form step `curious`.
- To let people reach a person, set the top-level `handoff: {when: [user_request]}` (add max_reasks or disengaged
  when asked); to hand off in a situation ("refunds over $100"), add a rule with `handoff: true` whose `then` says
  what to tell them, and keep the top-level `handoff`. Never add `handoff` or `exit` as flow steps: the runtime runs
  them.
- Begin the YAML with one comment line per change, in plain words for a non-technical owner (no key names):
  `# change: <what changed>`."""

#: what the owner asks, at most
EDIT_LIMIT = 2_000


async def edit_policy(
    llm: LLMClient, models: ModelConfig, yaml: str, instruction: str, tenant: str, datasets: Mapping[str, DatasetKind]
) -> tuple[Draft, list[str]]:
    """The policy changed as the owner asks in plain words, checked by the build and repaired from its errors. Returns
    the draft (its change lines removed) and the change lines. Never registered or saved."""
    content = f"Catalog:\n{catalog(tenant, datasets)}\n\n" + untrusted("policy", yaml) + "\n\n" + untrusted("request", instruction[:EDIT_LIMIT])
    d = await _until_it_builds(llm, models, "policy_edit", EDIT, [{"role": "user", "content": content}], tenant, datasets)
    d.yaml, changes = new_changes(yaml, d.yaml)
    header = _header(yaml)
    if header and not d.yaml.startswith("#"):  # the file's opening comments are the owner's: kept as written
        d.yaml = header + d.yaml
    return d, changes


def new_changes(before: str, after: str) -> tuple[str, list[str]]:
    """The change lines `after` adds, taken out of it (they are shown, not kept). Lines already in `before` (e.g. from a
    revision suggested by ratings) are not this edit's: they stay where they are."""
    earlier = {m.strip() for m in CHANGE.findall(before)}
    changes, kept = [], []
    for line in after.splitlines(keepends=True):
        m = CHANGE.match(line)
        if m and m.group(1).strip() not in earlier:
            changes.append(m.group(1).strip())
        else:
            kept.append(line)
    return "".join(kept).lstrip("\n"), changes


def _header(yaml: str) -> str:
    """The comment lines a policy file opens with."""
    lines = []
    for line in yaml.splitlines(keepends=True):
        if not line.lstrip().startswith("#"):
            break
        lines.append(line)
    return "".join(lines)


REVISE = """You revise an agent policy (YAML) so the agent stops making the mistakes people reported when they rated its
conversations. Reply with the whole revised YAML only.
- The reply is the policy file itself: top-level keys (`agent:`, `charter:`, `flow:`...), never nested under another key.
- Change as little as possible: keep every key, step and comment the feedback does not touch, and the same `agent`.
- Fix the cause, not the one conversation. Prefer, in this order: reword the `then` of a rule that did not do its job;
  add a rule (`rules:` items with `name` in snake_case, `when` the situation in the user's words, `then` what the reply
  must do); tighten the charter's out-of-scope sentence; reword a field's `ask` or `expect`.
- Never remove guardrails, the consent step, memory consent or handoff.
- Begin the YAML with one comment line per change: `# change: <what changed> (for: <the feedback it answers>)`.
- If the feedback asks for nothing the policy can fix, return it unchanged with no change lines."""

#: change lines the revision starts with
CHANGE = re.compile(r"^#\s*change:\s*(.+)$", re.MULTILINE)


async def revise_policy(
    llm: LLMClient, models: ModelConfig, yaml: str, feedback: list[str], tenant: str, datasets: Mapping[str, DatasetKind]
) -> tuple[Draft, list[str]]:
    """The policy rewritten to answer what people reported (`feedback`, one line per problem), checked by the build and
    repaired from its errors like a draft. Returns the draft and its change lines. Never registered or saved."""
    content = untrusted("policy", yaml) + "\n\n" + untrusted("feedback", "\n".join(f"- {f}" for f in feedback))
    d = await _until_it_builds(llm, models, "policy_revise", REVISE, [{"role": "user", "content": content}], tenant, datasets)
    return d, [m.strip() for m in CHANGE.findall(d.yaml)]


async def _until_it_builds(
    llm: LLMClient, models: ModelConfig, purpose: str, system: str, messages: list[dict[str, str]], tenant: str, datasets: Mapping[str, DatasetKind]
) -> Draft:
    """The model's YAML, fed the build's errors (YAML paths) until it builds or the attempts run out."""
    text, errors = "", list[PolicyError]()
    for attempt in range(1, DRAFT_ATTEMPTS + 1):
        req = LLMRequest(purpose, models.generate, system, messages, max_tokens=2000, effort="medium")
        text = _yaml_only(await llm.complete(req))
        try:
            build(text, tenant=tenant, datasets=datasets)
            log.info("policy drafted", purpose=purpose, attempts=attempt)
            return Draft(text, [], attempt)
        except PolicyValidationError as e:
            errors = e.errors
        log.info("draft repair", purpose=purpose, attempt=attempt, errors=len(errors), first=[f"{x.path}: {x.message}"[:160] for x in errors[:3]])
        problems = "\n".join(f"- {x.path}: {x.message}" for x in errors)
        hint = ""
        if any("valid string" in x.message for x in errors):  # YAML reads bare yes/no/on/off as true/false
            hint = 'A value that should be text was read as true/false/null: quote it ("yes", "no").\n'
        if any("mapping values are not allowed" in x.message for x in errors):  # "Out of scope: ..." on one bare line
            hint += "Text with a colon in it must be quoted or written as a folded block (`charter: >` and the text on the next lines).\n"
        if any(x.path.startswith("handoff.when") for x in errors):  # a situation written as a trigger
            hint += (
                "handoff.when lists only user_request, max_reasks, disengaged, tool_unavailable. To hand off in a situation, "
                "add a rule with `handoff: true` instead.\n"
            )
        fix = f"The build rejected it:\n{problems}\n{hint}Reply with the corrected YAML only."
        messages = [*messages, {"role": "assistant", "content": text}, {"role": "user", "content": fix}]
    return Draft(text, errors, DRAFT_ATTEMPTS)

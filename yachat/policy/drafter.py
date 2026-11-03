"""Draft a policy from a plain description: the model writes YAML from the registry's catalog, the real build checks
it, and its errors (YAML paths) are fed back. Attempts are counted in code; the draft is never registered or saved."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from yachat.datasets.models import DatasetKind, field_dataset_kind
from yachat.logging import get_logger
from yachat.policy.compiler import build
from yachat.policy.errors import PolicyError, PolicyValidationError
from yachat.registry.core import REGISTRY
from yachat.runtime.llm import LLMClient, LLMRequest, ModelConfig, untrusted

log = get_logger("drafter")

#: the first draft plus repairs from the build's errors
DRAFT_ATTEMPTS = 3

SYSTEM = """You write agent policies: one YAML file that defines a goal-oriented chatbot. Reply with the YAML only.
Keys (no others): agent, charter, languages, tools, flow, guardrails, limits, memory, mode.
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


def _yaml_only(text: str) -> str:
    fenced = re.search(r"```(?:ya?ml)?\n(.*?)```", text, re.DOTALL)
    return (fenced.group(1) if fenced else text).strip() + "\n"


async def draft_policy(llm: LLMClient, models: ModelConfig, description: str, tenant: str, datasets: Mapping[str, DatasetKind]) -> Draft:
    messages = [{"role": "user", "content": f"Catalog:\n{catalog(tenant, datasets)}\n\n" + untrusted("description", description)}]
    text, errors = "", list[PolicyError]()
    for attempt in range(1, DRAFT_ATTEMPTS + 1):
        req = LLMRequest("policy_draft", models.generate, SYSTEM, messages, max_tokens=2000, effort="medium")
        text = _yaml_only(await llm.complete(req))
        try:
            build(text, tenant=tenant, datasets=datasets)
            log.info("policy drafted", attempts=attempt)
            return Draft(text, [], attempt)
        except PolicyValidationError as e:
            errors = e.errors
        log.info("draft repair", attempt=attempt, errors=len(errors), first=[f"{x.path}: {x.message}"[:160] for x in errors[:3]])
        problems = "\n".join(f"- {x.path}: {x.message}" for x in errors)
        hint = ""
        if any("valid string" in x.message for x in errors):  # YAML reads bare yes/no/on/off as true/false
            hint = 'A value that should be text was read as true/false/null: quote it ("yes", "no").\n'
        fix = f"The build rejected it:\n{problems}\n{hint}Reply with the corrected YAML only."
        messages = [*messages, {"role": "assistant", "content": text}, {"role": "user", "content": fix}]
    return Draft(text, errors, DRAFT_ATTEMPTS)

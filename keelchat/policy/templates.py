"""Bot types: starting policies for a new agent. The built-in ones are the example policies; a tenant can save its own."""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from keelchat.state.models import PolicyTemplate

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"
AGENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
#: the top-level `agent:` line, with any trailing comment
_AGENT_LINE = re.compile(r"^agent:[ \t]*[^\n#]*", re.M)

BLANK = """agent: my-bot
charter: >
  Say what the bot helps with, its tone, and what is out of scope.
flow:
  - greet_consent
"""

#: (id, name, description, example file or None for the blank policy)
BUILTIN = [
    ("blank", "Blank", "A charter and a consent step; add steps from there.", None),
    ("support", "Support assistant", "Consent, a short form (order number, issue), answers from a knowledge base.", "support_assistant.yaml"),
    ("hiring", "Hiring assistant", "Job search, pick a job, a validated application form, submit on the candidate's word.", "hiring_agent.yaml"),
    ("analyst", "Sales analyst", "Answers questions about a structured dataset with checked figures and recommendations.", "sales_analyst.yaml"),
    (
        "survey",
        "Feedback survey",
        "Say what you want to learn; it finds out in a natural conversation, with the NPS counted exactly.",
        "customer_feedback.yaml",
    ),
    ("classic-survey", "Fixed-question survey", "Questions asked in order, with conditions on earlier answers.", "nps_survey.yaml"),
    ("coach", "Career coach", "An open conversation that looks up skills and roles; no fixed flow.", "career_coach.yaml"),
]


@cache
def builtin_templates() -> list[PolicyTemplate]:
    out = []
    for tid, name, description, file in BUILTIN:
        path = EXAMPLES / file if file else None
        if path is not None and not path.is_file():  # a trimmed install without examples/
            continue
        yaml = path.read_text(encoding="utf-8") if path else BLANK
        out.append(PolicyTemplate(tenant="", id=tid, name=name, description=description, yaml=yaml, builtin=True))
    return out


def renamed(yaml: str, agent: str) -> str:
    """The policy with its `agent:` set to `agent`; everything else, comments included, unchanged."""
    if not AGENT_ID.match(agent):
        raise ValueError("an agent name is lowercase letters, digits and dashes, starting with a letter or digit")
    if _AGENT_LINE.search(yaml):
        return _AGENT_LINE.sub(f"agent: {agent}", yaml, count=1)
    return f"agent: {agent}\n{yaml}"

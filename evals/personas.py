"""Simulated users with personas. Uses DeepEval's ConversationSimulator when a judge model is configured; otherwise a
scripted conversation per persona, run against the runtime with the configured model and fake tools."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from evals.replay import ReplayCase

PERSONAS: dict[str, dict[str, Any]] = {
    "spanish_typos": {
        "description": "A Spanish speaker who makes typos and never switches language.",
        "script": ["hola, busco trabjo en austin", "sí", "enfermera en austin", "1", "Ana Ruiz", "12 Elm St, Austin TX 78704", "sí"],
    },
    "sms_shorthand": {
        "description": "An SMS user who writes shorthand.",
        "channel": "sms",
        "script": ["hi", "yes", "u hav wrk 2mrw nr me", "1", "Jo Lee", "9 Oak Ave, Dallas TX 75201", "yes"],
    },
    "chatty_non_serious": {
        "description": "Keeps asking off-goal questions and never answers.",
        "script": ["hello", "yes", "engineer", "1", "what about parking?", "do you have a gym?", "is there a cafeteria?", "and the dress code?"],
    },
    "returning_candidate": {
        "description": "Has a saved address from a previous session.",
        "user_id": "returning-1",
        "script": ["hello", "yes", "backend engineer in austin", "1", "Sam Returning", "yes", "yes"],
    },
}


@dataclass
class Persona:
    name: str
    description: str
    script: list[str]
    channel: str = "web"
    user_id: str | None = None
    expect: dict[str, Any] = dc_field(default_factory=dict)

    def as_case(self) -> ReplayCase:
        return ReplayCase(name=f"persona:{self.name}", messages=self.script, channel=self.channel, user_id=self.user_id, expect=self.expect)


def personas(names: list[str] | None = None) -> list[Persona]:
    out = []
    for n, p in PERSONAS.items():
        if names and n not in names:
            continue
        out.append(Persona(n, p["description"], list(p["script"]), p.get("channel", "web"), p.get("user_id"), dict(p.get("expect", {}))))
    return out


async def simulate_with_deepeval(persona: Persona, model_callback: Any, max_turns: int = 8) -> Any:
    """DeepEval ConversationSimulator; `model_callback(input, turns, thread_id) -> Turn`. Needs a judge model."""
    from deepeval.simulator import ConversationSimulator

    sim = ConversationSimulator(model_callback=model_callback, max_concurrent=1)
    return await sim.a_simulate(
        conversation_golden_list=[
            {"scenario": persona.description, "expected_outcome": "finishes the flow or exits cleanly", "user_description": persona.description}
        ],
        max_user_simulations=1,
        max_turns=max_turns,
    )

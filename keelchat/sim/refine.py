"""Test a survey with pretend customers, measure it, and improve it: the confidence loop.

A round plays the survey with a fixed set of model-played customers (happy, unhappy, chatty, distracted), each in its
own sandbox (fake tools, nothing sent or stored outside it). The round is measured in code from what was actually kept:
how many finished, and for each objective, how many conversations ended with an answer to it. That gives a confidence
score (0-100). Below the target, the model proposes a revised policy from the measured gaps and the stuck transcripts;
the build checks it, the same objectives (keys and measures) must survive, and the revision is played again. Rounds are
counted in code; the best-scoring version is returned, never one that scored lower."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from keelchat.capabilities.nodes.survey_answers import survey_questions_of
from keelchat.datasets.models import DatasetKind
from keelchat.logging import get_logger
from keelchat.policy.compiler import build
from keelchat.policy.drafter import draft_policy
from keelchat.runtime.llm import LLMClient, ModelConfig
from keelchat.sim.harness import sim_runtime, simulate

log = get_logger("refine")

PERSONAS: list[tuple[str, str]] = [
    ("happy", "A happy repeat customer. Answers briefly and kindly; gives a high score if asked."),
    ("unhappy", "An unhappy customer whose order arrived a week late. Curt, a little annoyed, but answers clear questions."),
    ("chatty", "A friendly customer who writes long replies that mix several topics in one message, and goes off on tangents."),
    ("busy", "A busy person who replies in two or three words, sometimes asks what this is for, and wants it over quickly."),
]
#: each part of the confidence score, in code
WEIGHTS = {"finished": 0.4, "answered": 0.45, "smooth": 0.15}
MAX_ROUNDS = 3


class Conversation(BaseModel):
    persona: str
    finished: bool
    exit_reason: str | None
    questions: int
    answered: list[str]
    transcript: list[dict[str, str]]


class Round(BaseModel):
    round: int
    score: int
    finished: float  # share of conversations that reached the end
    answered: dict[str, float]  # objective key -> share of conversations with an answer to it
    smooth: float  # share with no repeated question and no stall
    problems: list[str]
    yaml: str
    conversations: list[Conversation] = Field(default_factory=list)


class Refinement(BaseModel):
    rounds: list[Round]
    best: int  # index into rounds
    target: int


def _filled(v: Any) -> bool:
    return v not in (None, "", [], {})


async def play(llm: LLMClient, models: ModelConfig, policy_yaml: str, tenant: str, datasets: dict[str, DatasetKind], n: int) -> Round:
    """One round: every persona takes the survey; the result is measured from the answers actually kept."""
    policy = build(policy_yaml, tenant=tenant, datasets=datasets)
    keys = [q.key for q in survey_questions_of(policy.steps)]
    if not keys:
        raise ValueError("this agent has no survey or objectives to test")
    srt = sim_runtime(llm, models)
    await srt.register_policy(policy)
    report = await simulate(srt, policy, [{"name": name, "persona": text, "max_turns": 14} for name, text in PERSONAS], parallel=2)
    kept = {r.user_id: r for r in await srt.responses.list(policy.tenant, policy.agent)}
    convs = []
    for c in report.conversations:
        r = kept.get(f"sim-{c.name}")
        got = [k for k in keys if r is not None and _filled(r.answers.get(k))]
        convs.append(
            Conversation(persona=c.name, finished=c.completed, exit_reason=c.exit_reason, questions=c.turns, answered=got, transcript=c.transcript[-14:])
        )
    total = len(convs) or 1
    finished = sum(c.finished for c in convs) / total
    answered = {k: round(sum(k in c.answered for c in convs) / total, 2) for k in keys}
    stuck = [c for c in report.conversations if c.reasks >= 2 or c.exit_reason in ("max_reasks", "disengaged")]
    smooth = 1 - len(stuck) / total
    score = round(100 * (WEIGHTS["finished"] * finished + WEIGHTS["answered"] * (sum(answered.values()) / len(keys)) + WEIGHTS["smooth"] * smooth))
    problems = [f"{c.persona}: ended without finishing ({c.exit_reason or 'ran out of turns'})" for c in convs if not c.finished]
    problems += [f"objective {k!r} answered in {round(v * total)} of {total} conversations" for k, v in answered.items() if v < 0.75]
    problems += [f"{c.name}: asked the same thing again several times" for c in stuck]
    log.info("refine round", round=n, score=score, finished=finished, smooth=smooth)
    return Round(
        round=n, score=score, finished=round(finished, 2), answered=answered, smooth=round(smooth, 2), problems=problems, yaml=policy_yaml, conversations=convs
    )


IMPROVE = """Revise this survey policy so pretend customers finish it and every objective gets answered. Rules:
- Keep the same agent name and the same objective keys and measures (results are compared by them).
- You may reword `learn` and `ask` to be clearer and friendlier, reorder objectives, raise `max_probes` (up to 3) or
  `max_questions`, shorten the charter, and remove a consent step only if the description doesn't need it.
- Fix the problems measured below; don't add new objectives.
Measured problems:
{problems}
Conversations that went wrong (latest turns):
{transcripts}
The policy:
{yaml}"""


async def improve(llm: LLMClient, models: ModelConfig, r: Round, tenant: str, datasets: dict[str, DatasetKind]) -> str | None:
    """A revised policy aimed at the round's measured problems, or None when the model's draft doesn't build or changes
    the objectives."""
    bad = [c for c in r.conversations if not c.finished or len(c.answered) < len(r.answered)][:2]
    transcripts = "\n\n".join(f"[{c.persona}]\n" + "\n".join(f"customer: {t['user'][:300]}\nagent: {t['agent'][:300]}" for t in c.transcript[-6:]) for c in bad)
    description = IMPROVE.format(problems="\n".join(f"- {p}" for p in r.problems) or "- none", transcripts=transcripts or "(none)", yaml=r.yaml)
    draft = await draft_policy(llm, models, description, tenant, datasets)
    if draft.errors:
        log.info("refine draft rejected", errors=len(draft.errors))
        return None
    before = {(q.key, q.kind, q.scale_min, q.scale_max) for q in survey_questions_of(build(r.yaml, tenant=tenant, datasets=datasets).steps)}
    after = {(q.key, q.kind, q.scale_min, q.scale_max) for q in survey_questions_of(build(draft.yaml, tenant=tenant, datasets=datasets).steps)}
    if before != after:  # results must stay comparable: the same objectives, measured the same way
        log.info("refine draft changed the objectives; rejected")
        return None
    return draft.yaml


async def refine(
    llm: LLMClient, models: ModelConfig, policy_yaml: str, tenant: str, datasets: dict[str, DatasetKind], rounds: int = 2, target: int = 85
) -> Refinement:
    """Play, measure, improve, play again: up to `rounds` plays (counted in code), stopping once one reaches `target`."""
    rounds = max(1, min(rounds, MAX_ROUNDS))
    played = [await play(llm, models, policy_yaml, tenant, datasets, 1)]
    while len(played) < rounds and played[-1].score < target:
        best = max(played, key=lambda r: r.score)
        revised = await improve(llm, models, best, tenant, datasets)
        if revised is None:
            break
        played.append(await play(llm, models, revised, tenant, datasets, len(played) + 1))
    best_i = max(range(len(played)), key=lambda i: (played[i].score, -i))
    return Refinement(rounds=played, best=best_i, target=target)

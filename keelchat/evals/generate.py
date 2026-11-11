"""Generate an eval set from the policy: list what to cover (in code), let the model play a user aiming at each target,
check every conversation (keelchat.evals.checks), shorten the ones that fail, keep one per target reached.

What a case must achieve comes from the policy, never from the model: the model only writes what the user says.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from dataclasses import field as dc_field
from functools import partial
from pathlib import Path

import yaml

from keelchat.evals.checks import check, rules_by_name
from keelchat.evals.replay import GENERATED, PARALLEL, Expect, Fixtures, ReplayCase, ReplayResult, load_set, run_case
from keelchat.policy.compiler import CompiledPolicy

#: turns a played user gets to reach a target
TARGET_TURNS = 10
#: replays spent shortening one failing conversation
SHRINK_RUNS = 6
#: tries per target before it is reported as not reached
ROUNDS = 2
#: seconds one try at a target (play, check, shorten) may take; past it the try counts as not reached
TRY_TIMEOUT_S = 900.0
STYLE = " Write short, natural messages like a real person, one at a time."


@dataclass(frozen=True)
class Target:
    #: also the coverage key `covered()` reports
    id: str
    #: what the played user does
    goal: str
    expect: Expect = dc_field(default_factory=Expect)


def targets(policy: CompiledPolicy) -> list[Target]:
    """What a set for this policy should cover: the flow, bad answers, each rule and a near miss, scope, handoff,
    giving up, and each language."""
    out: list[Target] = []
    if policy.steps:
        out.append(Target("completed", "Goes along with everything and gives a valid, believable answer to every question until done.", Expect(completed=True)))
        out.append(
            Target("disengaged", "Agrees to start, then keeps changing the subject to unrelated small talk instead of answering.", Expect(completed=False))
        )
        for st in policy.steps:
            for f in st.settings.get("fields") or []:
                if isinstance(f, dict) and f.get("name"):
                    out.append(
                        Target(
                            f"reask:{f['name']}",
                            f"Cooperates, but when asked for {f['name'].replace('_', ' ')}, first gives a vague or unusable answer, "
                            "then a valid one when asked again.",
                        )
                    )
                    if st.settings.get("curious"):
                        out.append(
                            Target(
                                f"probe:{f['name']}",
                                f"Cooperates, but answers {f['name'].replace('_', ' ')} in three or four words with no details, "
                                "then gives the details when asked.",
                            )
                        )
    for name, rule in rules_by_name(policy).items():
        out.append(Target(f"rule:{name}", f"Cooperates, and early on brings up this situation in their own words: {rule.when}", Expect(rules_fired=[name])))
        out.append(
            Target(
                f"rule_miss:{name}",
                f"Cooperates, and mentions something close to, but clearly not, this situation: {rule.when}",
                Expect(rules_not_fired=[name]),
            )
        )
    if policy.small_talk != "off":
        out.append(
            Target(
                "small_talk",
                "Cooperates, but before answering the second question makes friendly small talk (asks how the assistant's "
                "day is, or jokes about the weather), then answers everything.",
                Expect(completed=True) if policy.steps else Expect(),
            )
        )
    out.append(Target("out_of_scope", f"Asks for help with something this assistant should not do, given its purpose: {policy.charter.strip()}"))
    if policy.handoff and "user_request" in policy.handoff.when:
        out.append(
            Target("handed_off", "After a message or two, asks to talk to a real person and keeps asking until handed over.", Expect(exit_reason="handed_off"))
        )
    for lang in policy.languages[1:]:
        out.append(Target(f"language:{lang}", f"Writes only in the language with code '{lang}', from the first message.", Expect(language=lang)))
    if not policy.steps and policy.allowed_tools:
        out.append(Target("uses_tools", "Asks two or three ordinary questions this assistant exists to answer."))
    return out


def covered(policy: CompiledPolicy, r: ReplayResult) -> set[str]:
    """Coverage keys this conversation reached, read from the session and the turn log (never from the model)."""
    s = r.state
    keys = {f"step:{n}" for n, status in s.steps.items() if status == "complete"}
    keys |= {f"reask:{n}" for t in r.turns for n in t.reasked}  # the session clears a count once the field is answered
    keys |= {f"probe:{n}" for t in r.turns for n in t.probed}
    keys |= {f"rule:{n}" for t in r.turns for n in t.rules}
    if s.completed:
        keys.add("completed")
    if s.exit_reason:
        keys.add(s.exit_reason)
    if s.language != policy.languages[0]:
        keys.add(f"language:{s.language}")
    if any(t.tools for t in r.turns):
        keys.add("uses_tools")
    return keys


def _reached(t: Target, r: ReplayResult, cov: set[str]) -> bool:
    if t.id.startswith("rule_miss:") or t.id in ("out_of_scope", "small_talk"):  # the user's goal; the agent's part is checked
        return len(r.turns) >= 2
    return t.id in cov


async def shrink(messages: list[str], fails: Callable[[list[str]], Awaitable[bool]], runs: int = SHRINK_RUNS) -> tuple[list[str], bool]:
    """The shortest script (dropping one message at a time, from the end) that still fails. Returns the script and
    whether the failure reproduced at all as a script."""
    if not await fails(messages):
        return messages, False
    used, i = 1, len(messages) - 1
    while i >= 0 and used < runs and len(messages) > 1:
        trial = messages[:i] + messages[i + 1 :]
        used += 1
        if await fails(trial):
            messages = trial
        i -= 1
    return messages, True


@dataclass
class GenerateReport:
    agent: str
    targets: list[str]
    cases: list[ReplayCase] = dc_field(default_factory=list)
    covered: set[str] = dc_field(default_factory=set)
    #: target → why the last try missed it (the played user may not have done it, or the agent did not respond to it)
    not_reached: dict[str, str] = dc_field(default_factory=dict)
    #: case name → what it failed
    failures: dict[str, list[str]] = dc_field(default_factory=dict)

    def summary(self) -> str:
        reached = [t for t in self.targets if t not in self.not_reached]
        lines = [
            f"{self.agent}: {len(self.cases)} cases · targets {len(reached)}/{len(self.targets)} · {len(self.failures)} failing",
            *(f"  FAIL {n}: {'; '.join(v)}" for n, v in self.failures.items()),
        ]
        if self.not_reached:
            lines += [f"  not reached {t}: {why}" for t, why in self.not_reached.items()]
        if steps := sorted(k for k in self.covered if k.startswith("step:")):
            lines.append(f"  steps completed somewhere: {', '.join(s.removeprefix('step:') for s in steps)}")
        return "\n".join(lines)


def case_name(target_id: str) -> str:
    """The case a target's test is saved as."""
    return "gen_" + re.sub(r"[^a-z0-9]+", "_", target_id.lower()).strip("_")


async def generate(
    policy: CompiledPolicy,
    fixtures: Fixtures | None = None,
    judge: str | None = None,
    only: list[str] | None = None,
    rounds: int = ROUNDS,
    parallel: int = PARALLEL,
    progress: Callable[[str], None] | None = None,
) -> GenerateReport:
    """Play each target (up to `rounds` tries), check it, shorten failures. `only` keeps targets whose id starts with
    one of its entries. `judge` adds the judged checks. `progress` gets a line as each target finishes."""
    todo = [t for t in targets(policy) if not only or any(t.id.startswith(o) for o in only)]
    report = GenerateReport(policy.agent, [t.id for t in todo])
    gate = asyncio.Semaphore(parallel)

    async def fails(t: Target, messages: list[str], names: set[str]) -> bool:
        """The script, replayed, fails one of the same checks."""
        r = await run_case(policy, ReplayCase(name=case_name(t.id), messages=messages, expect=t.expect), fixtures=fixtures)
        return bool(await check(policy, r, judge, only=names))

    async def attempt(t: Target) -> str | None:
        """One try: None when the target is settled (kept or failing), else why it was missed."""
        persona = ReplayCase(name=case_name(t.id), persona=t.goal + STYLE, max_turns=TARGET_TURNS, expect=t.expect)
        r = await run_case(policy, persona, fixtures=fixtures)
        # an unmet expectation may be the played user's doing: only the checks mark the agent as failing
        found = [v for v in await check(policy, r, judge) if v.check != "expect"]
        cov = covered(policy, r)
        report.covered |= cov
        script = [x.user for x in r.turns]
        if found:
            names = {v.check for v in found}
            short, reproduced = await shrink(script, partial(fails, t, names=names))
            note = f"fails {', '.join(sorted(names))}" + ("" if reproduced else " (as played; did not reproduce as a script)")
            # the target's expectation is dropped: a shortened script no longer plays the whole target
            report.cases.append(ReplayCase(name=case_name(t.id), messages=short, note=note))
            report.failures[case_name(t.id)] = [str(v) for v in found]
            if progress:
                progress(f"FAIL {t.id}: {', '.join(sorted(names))} ({len(short)} messages)")
            return None
        if not r.expectation_failures and _reached(t, r, cov):
            # the script must reach the target without the played user steering, and still pass: replay it once
            replay = await run_case(policy, ReplayCase(name=case_name(t.id), messages=script, expect=t.expect), fixtures=fixtures)
            stable = not await check(policy, replay, judge) and _reached(t, replay, covered(policy, replay))
            note = f"covers {t.id}" if stable else f"covered {t.id} as played; the script alone does not, so nothing is expected"
            report.cases.append(ReplayCase(name=case_name(t.id), messages=script, expect=t.expect if stable else Expect(), note=note))
            if progress:
                progress(f"ok   {t.id} ({len(script)} messages)")
            return None
        return "; ".join(r.expectation_failures) or f"did not reach it in {len(r.turns)} turns"

    async def one(t: Target) -> None:
        why: str | None = "no try finished"
        for _ in range(rounds):
            async with gate:
                try:
                    why = await asyncio.wait_for(attempt(t), TRY_TIMEOUT_S)
                except TimeoutError:
                    why = f"a try took over {TRY_TIMEOUT_S:.0f}s"
                except Exception as e:  # a provider error ends this try, never the whole run
                    why = f"the try failed: {e.__class__.__name__}"
            if why is None:
                return
        report.not_reached[t.id] = why or ""
        if progress:
            progress(f"miss {t.id}: {why}")

    await asyncio.gather(*(one(t) for t in todo))
    order = {case_name(t): i for i, t in enumerate(report.targets)}
    report.cases.sort(key=lambda c: order[c.name])
    report.not_reached = {t: report.not_reached[t] for t in report.targets if t in report.not_reached}
    return report


def write(agent_dir: str | Path, policy_path: str | Path, report: GenerateReport) -> Path:
    """generated.yaml: this run's targets are replaced, cases for other targets (an earlier run with `--only`) kept;
    review it as a diff. Also a thresholds.yaml naming the policy if the folder has none, so `keelchat evals run|check`
    work on it at once."""
    d = Path(agent_dir)
    d.mkdir(parents=True, exist_ok=True)
    out = d / GENERATED
    redone = {case_name(t) for t in report.targets}
    kept = [c for c in load_set(out).cases if c.name not in redone] if out.is_file() else []
    cases = [c.model_dump(mode="json", exclude_defaults=True) for c in [*kept, *report.cases]]
    out.write_text(f"# keelchat evals generate: {report.summary().splitlines()[0]}\n" + yaml.safe_dump({"cases": cases}, sort_keys=False, allow_unicode=True))
    th = d / "thresholds.yaml"
    if not th.is_file():
        th.write_text(yaml.safe_dump({"policy": os.path.relpath(Path(policy_path).resolve(), d.resolve()), "minimums": {}}, sort_keys=False))
    return out


def main(policy_path: str, agent_dir: str | None = None, only: list[str] | None = None, rounds: int | None = None) -> int:
    """`keelchat evals generate`: writes the set, prints the summary; 1 when a generated case fails."""
    from keelchat.evals.judge import judge_model_from_env
    from keelchat.policy.compiler import build

    policy = build(Path(policy_path).read_text())
    d = Path(agent_dir) if agent_dir else Path("evals") / policy.agent.replace("-", "_")
    fixtures = load_set(d / "replay.yaml").fixtures if (d / "replay.yaml").is_file() else None
    judge = judge_model_from_env()
    todo = [t.id for t in targets(policy) if not only or any(t.id.startswith(o) for o in only)]
    print(f"{policy.agent}: {len(todo)} targets, judge {judge or 'off (code checks only)'}", file=sys.stderr, flush=True)
    report = asyncio.run(generate(policy, fixtures, judge, only, rounds or ROUNDS, progress=lambda line: print(line, file=sys.stderr, flush=True)))
    out = write(d, policy_path, report)
    print(report.summary())
    print(f"wrote {out}")
    return 1 if report.failures else 0

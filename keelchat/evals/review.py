"""Conversations a person rated in the console (`Review`) as eval data: each rated reply is a calibration label
(`labels.yaml`), and each conversation with a failed reply or a low score is a replay case (`replay.yaml`), so a
problem a person found is checked on every change after it is fixed."""

from __future__ import annotations

import re

import yaml

from keelchat.state.models import Review

#: an overall score at or below this makes the conversation a replay case even with no failed reply
LOW_SCORE = 2
#: most ratings an export reads
EXPORT_LIMIT = 10_000
#: most problems one policy revision is asked to answer (newest first)
FEEDBACK_LIMIT = 30


def _latest(reviews: list[Review]) -> list[Review]:
    """The newest rating of each conversation (rating a test chat again after it grew saves a new one). `reviews` is
    oldest first."""
    by: dict[tuple[str, str], Review] = {}
    for r in reviews:
        by[(r.source, r.conversation)] = r
    return sorted(by.values(), key=lambda r: r.created_at)


def labels_yaml(reviews: list[Review]) -> str:
    """One label per rated reply: the conversation up to that reply, the check, the person's verdict. `reviews` is
    oldest first."""
    labels: list[dict[str, object]] = []
    skipped = 0
    for r in _latest(reviews):
        for x in sorted(r.replies, key=lambda x: (x.turn, x.check)):
            turns = [{"user": t.user, "agent": t.agent} for t in r.turns[: x.turn]]
            if not all(t["user"] and t["agent"] for t in turns):
                skipped += 1  # a label needs every message of its context
                continue
            note = "; ".join(n for n in (x.note, f"{r.source} {r.conversation}") if n)
            labels.append({"check": x.check, "turns": turns, "pass": x.ok, "note": note})
    left_out = f" ({skipped} left out: an empty message in the conversation)" if skipped else ""
    return _dump("labels", f"keelchat review: replies a person rated (keelchat evals calibrate){left_out}", labels)


def cases_yaml(reviews: list[Review]) -> str:
    """One replay case per conversation a person failed: its messages up to the last failed reply (all of them when only
    the overall score is low). The checks it failed are in `note`; `keelchat evals check` judges them again."""
    cases: list[dict[str, object]] = []
    for r in _latest(reviews):
        failed = [x for x in r.replies if not x.ok]
        if not failed and (r.score is None or r.score > LOW_SCORE):
            continue
        upto = max((x.turn for x in failed), default=len(r.turns))
        why = [f"{x.check} on turn {x.turn}{f' ({x.note})' if x.note else ''}" for x in sorted(failed, key=lambda x: x.turn)]
        if r.score is not None and r.score <= LOW_SCORE:
            why.append(f"scored {r.score}/5{f' ({r.note})' if r.note else ''}")
        cases.append({"name": _name(r), "messages": [t.user for t in r.turns[:upto]], "note": f"a person failed it: {'; '.join(why)}"})
    return _dump("cases", "keelchat review: conversations a person failed (add to evals/<agent>/replay.yaml)", cases)


def feedback(reviews: list[Review]) -> list[str]:
    """What people reported, one line per failed reply and per low-scored conversation with a note, newest first, for a
    policy revision. `reviews` is newest first."""
    out: list[str] = []
    for r in _latest(list(reversed(reviews)))[::-1]:
        for x in sorted((x for x in r.replies if not x.ok), key=lambda x: x.turn):
            t = r.turns[x.turn - 1]
            check = "kept to the charter's scope" if x.check == "in_scope" else f"followed rule {x.check.partition(':')[2]}"
            why = f"; the rater said: {x.note}" if x.note else ""
            out.append(f'Not {check}. User: "{_clip(t.user)}" Agent: "{_clip(t.agent)}"{why}')
        if r.score is not None and r.score <= LOW_SCORE and r.note:
            out.append(f"A conversation scored {r.score}/5; the rater said: {r.note}")
    return out[:FEEDBACK_LIMIT]


def _clip(text: str, n: int = 300) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _name(r: Review) -> str:
    return "review_" + re.sub(r"[^a-z0-9]+", "_", f"{r.conversation}_{r.id.removeprefix('rev_')}".lower()).strip("_")


def _dump(key: str, header: str, rows: list[dict[str, object]]) -> str:
    return f"# {header}: {len(rows)}\n" + yaml.safe_dump({key: rows}, sort_keys=False, allow_unicode=True, width=120)

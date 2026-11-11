"""Findings (ported from Verlox's tabulator): numbers counted in code over the people who were asked; themes whose
quotes are always people's own words."""

from __future__ import annotations

import csv
import io
from pathlib import Path

from httpx import ASGITransport, AsyncClient

from keelchat.api.app import create_app
from keelchat.capabilities.nodes.survey import SurveyConfig
from keelchat.policy.compiler import build
from keelchat.runtime.findings import tabulate, themes
from keelchat.state.models import SurveyResponse
from keelchat.state.store import InMemoryTenantToolStore
from tests.helpers import RecordingLLM, make_runtime

SURVEY = (Path(__file__).parents[1] / "examples" / "nps_survey.yaml").read_text()
QUESTIONS = {q.key: q for s in build(SURVEY).steps if s.node == "survey" for q in SurveyConfig.model_validate(dict(s.settings)).questions}


def resp(n: int, **answers: object) -> SurveyResponse:
    return SurveyResponse(tenant="default", agent="nps-survey", policy_hash="h", session_id=f"s{n}", user_id=f"u{n}", answers=answers)


RS = [
    resp(0, nps=8, channel="other", priorities=["price", "quality"], best_part='=HYPERLINK("http://evil.example")'),
    resp(1, nps=10, channel="a_friend", priorities=["quality", "price"], best_part="The glaze is beautiful and it arrived fast."),
    resp(2, nps=9, channel="search_engine", priorities=["quality", "delivery_speed"], best_part="Lovely colours, well packed."),
    resp(3, nps=3, channel="a_friend", priorities=["price", "quality"], what_went_wrong="It arrived two weeks late and the handle was chipped."),
    resp(4, nps=6, channel=None, priorities=None, what_went_wrong="Delivery took forever, nobody answered my emails."),
]


def test_numbers_are_counted_in_code_over_who_was_asked():
    nps = tabulate(QUESTIONS["nps"], RS)
    assert nps.n_asked == 5 and nps.mean == 7.2 and nps.nps == 0.0  # 2 promoters, 1 passive, 2 detractors
    ch = tabulate(QUESTIONS["channel"], RS)
    assert ch.n_asked == 4 and ch.counts[0].key == "a_friend" and ch.counts[0].n == 2 and ch.counts[0].share == 0.5  # s4 skipped it
    pr = tabulate(QUESTIONS["priorities"], RS)
    assert {c.key: c.n for c in pr.counts}["quality"] == 2  # ranked first
    assert tabulate(QUESTIONS["what_went_wrong"], RS).n_asked == 2  # only low scorers were asked


async def test_theme_quotes_are_peoples_own_words():
    class Inventing(RecordingLLM):
        async def structured(self, req, schema):
            return {
                "themes": [
                    {"theme": "Late delivery", "summary": "Orders arrived late.", "answer_ids": ["a1", "a2", "a99"]},
                    {"theme": "Invented", "summary": "Made up.", "answer_ids": ["a42"]},
                ]
            }

    ts, err = await themes(QUESTIONS["what_went_wrong"], RS, Inventing(), "m")
    assert not err and [t.theme for t in ts] == ["Late delivery"]  # a theme with no real answer behind it is dropped
    assert ts[0].n == 2 and {q.text for q in ts[0].quotes} == {RS[3].answers["what_went_wrong"], RS[4].answers["what_went_wrong"]}


async def test_findings_and_export_over_the_api_with_the_real_model():
    rt, _ = await make_runtime(SURVEY)
    for r in RS:
        await rt.responses.add(r)
    app = create_app(rt, tool_store=InMemoryTenantToolStore())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        f = (await c.get("/v1/tenants/default/agents/nps-survey/findings")).json()
        assert f["respondents"] == 5
        calls = len(rt.llm.calls)
        assert (await c.get("/v1/tenants/default/agents/nps-survey/findings")).json() == f and len(rt.llm.calls) == calls  # reused
        recs = (await c.get("/v1/tenants/default/agents/nps-survey/findings/recommendations")).json()
        assert recs["overall_nps"] == 0.0 and isinstance(recs["fix"], list)
        for r in recs["fix"]:  # every fix is backed by unhappy customers and their own words
            assert r["detractors"] >= 1 and r["action"] and all(q["text"] in {x.answers.get("what_went_wrong") for x in RS} for q in r["quotes"])
        wrong = next(q for q in f["questions"] if q["key"] == "what_went_wrong")
        quotes = {q["text"] for t in wrong["themes"] for q in t["quotes"]}
        assert quotes <= {RS[3].answers["what_went_wrong"], RS[4].answers["what_went_wrong"]} and (wrong["themes"] or wrong["themes_error"])
        r = await c.get("/v1/tenants/default/agents/nps-survey/responses.csv")
        rows = list(csv.reader(io.StringIO(r.text)))
        assert rows[0][:3] == ["response_id", "completed_at", "nps"] and len(rows) == 6
        assert "quality; price" in rows[2]
        assert any(cell.startswith("'=HYPERLINK") for cell in rows[1])  # a formula typed as an answer is quoted
        assert (await c.get("/v1/tenants/default/agents/hiring-agent/findings")).status_code in (404, 422)
        d = (await c.delete("/v1/users/u3/memory")).json()
        assert d["survey_responses_deleted"] == 1 and len(await rt.responses.list("default", "nps-survey")) == 4


def test_nps_is_only_for_the_recommend_question():
    """A 0-10 satisfaction question gets an average, not an NPS label; the recommend question gets NPS whatever its key."""
    from keelchat.capabilities.nodes.survey_answers import SurveyQuestion

    sat = SurveyQuestion.model_validate({"key": "satisfaction", "kind": "scale", "prompt": "How satisfied were you?", "scale_min": 0, "scale_max": 10})
    rec = SurveyQuestion.model_validate({"key": "likely", "kind": "scale", "prompt": "How likely are you to recommend us?", "scale_min": 0, "scale_max": 10})
    rs = [resp(0, satisfaction=10, likely=10), resp(1, satisfaction=2, likely=2)]
    assert tabulate(sat, rs).nps is None and tabulate(sat, rs).mean == 6.0
    assert tabulate(rec, rs).nps == 0.0
    assert tabulate(QUESTIONS["nps"], RS).nps == 0.0  # the key "nps" is enough


async def test_the_newest_responses_are_kept_when_there_are_too_many():
    from datetime import UTC, datetime, timedelta

    from keelchat.state.store import InMemoryResponseStore

    store, t0 = InMemoryResponseStore(), datetime(2026, 1, 1, tzinfo=UTC)
    for n in range(5):
        await store.add(resp(n, nps=n).model_copy(update={"completed_at": t0 + timedelta(days=n)}))
    got = await store.list("default", "nps-survey", limit=2)
    assert [r.session_id for r in got] == ["s3", "s4"]  # the latest two, oldest first


async def test_open_answers_lose_personal_details_before_themes_and_quotes(monkeypatch):
    from keelchat.runtime import findings as f

    seen: list[tuple[str, str]] = []

    async def fake_group(context, items, llm, model, purpose="findings_themes"):
        seen.extend(items)
        return []

    monkeypatch.setattr(f, "group_quotes", fake_group)
    rs = [resp(0, what_went_wrong="Late again. Email me at ana@example.com or call +1 415 555 0100 or +44 20 7946 0958.")]
    await f.themes(QUESTIONS["what_went_wrong"], rs, None, "m")
    assert seen and "ana@example.com" not in seen[0][1] and "555 0100" not in seen[0][1] and "7946" not in seen[0][1] and "Late again." in seen[0][1]


def test_aggregates_trend_and_segments_are_counted_in_code():
    """NPS by week and by segment (campaign, product, buyer, channel, language), from responses, with empty weeks shown
    and small groups flagged."""
    from datetime import UTC, datetime, timedelta

    from keelchat.runtime.findings import aggregates

    monday = datetime(2026, 9, 7, 10, tzinfo=UTC)

    def at(n: int, week: int, score: int, complete: bool = True, **seg: str) -> SurveyResponse:
        return resp(n, nps=score).model_copy(update={"completed_at": monday + timedelta(weeks=week, days=n % 3), "segment": seg, "complete": complete})

    rs = [
        at(0, 0, 10, product="Sourdough", buyer="repeat", channel="email", language="en"),
        at(1, 0, 3, product="Sourdough", buyer="first", channel="email", language="en"),
        at(2, 0, 9, product="Croissant", buyer="repeat", channel="web", language="es"),
        at(3, 2, 2, False, product="Croissant", buyer="first", channel="email", language="en"),  # stopped after the score
        at(4, 2, 10, product="Croissant", buyer="repeat", channel="email", language="en"),
    ]
    a = aggregates(list(QUESTIONS.values()), rs, by="week")
    assert a.question == "nps" and a.respondents == 5 and a.completed == 4
    assert [p.start for p in a.trend] == ["2026-09-07", "2026-09-14", "2026-09-21"]  # the empty week is shown
    assert [p.respondents for p in a.trend] == [3, 0, 2] and a.trend[0].nps == 33.3 and a.trend[1].nps is None and a.trend[2].nps == 0.0
    buyer = {s.value: s for s in a.segments["buyer"]}
    assert buyer["repeat"].nps == 100.0 and buyer["repeat"].respondents == 3 and buyer["first"].nps == -100.0
    assert buyer["first"].small  # fewer than 5 people: shown, but flagged
    assert {s.value for s in a.segments["language"]} == {"en", "es"} and "product" in a.segments
    months = aggregates(list(QUESTIONS.values()), rs, by="month")
    assert [p.start for p in months.trend] == ["2026-09-01"]


async def test_what_to_fix_first_is_ranked_in_code_from_unhappy_customers():
    """Themes become recommendations: ranked by how many detractors raise them (counted in code), each with the NPS of
    the people in it and their own words; the model only words the action, and an action with a number is dropped."""
    from keelchat.runtime.findings import Findings, QuestionFindings, Quote, Theme, recommendations
    from tests.helpers import RecordingLLM

    rs = [resp(n, nps=s) for n, s in enumerate([2, 3, 4, 10, 9, 9, 5, 10])]
    ids = [r.id for r in rs]
    late = Theme(
        theme="Late delivery", summary="Orders arrived late.", n=4, quotes=[Quote(response_id=ids[0], text="Took two weeks.")], response_ids=ids[0:3] + [ids[6]]
    )
    taste = Theme(
        theme="Great taste", summary="People love the bread.", n=3, quotes=[Quote(response_id=ids[3], text="Best sourdough in town.")], response_ids=ids[3:6]
    )
    box = Theme(theme="Packaging", summary="Boxes were crushed.", n=1, quotes=[Quote(response_id=ids[1], text="The box was crushed.")], response_ids=[ids[1]])
    found = Findings(
        agent="nps-survey", respondents=8, completed=8,
        questions=[QuestionFindings(key="what_went_wrong", prompt="What could we do better?", kind="open", n_asked=8, themes=[box, late, taste])],
    )  # fmt: skip
    recs = await recommendations(
        found, list(QUESTIONS.values()), rs, RecordingLLM(), __import__("keelchat.runtime.llm", fromlist=["ModelConfig"]).ModelConfig.from_env().generate
    )
    fix = [r for r in recs.fix]
    assert [r.theme for r in fix] == ["Late delivery", "Packaging"]  # 4 detractors before 1
    assert fix[0].detractors == 4 and fix[0].people == 4 and fix[0].nps == -100.0 and fix[0].quotes[0].text == "Took two weeks."
    assert [r.theme for r in recs.keep] == ["Great taste"] and recs.keep[0].nps == 100.0
    assert all(r.action and not any(ch.isdigit() for ch in r.action) for r in [*recs.fix, *recs.keep])
    assert recs.overall_nps == 0.0  # 4 promoters and 4 detractors of 8

    # people who never gave a score are not "happy": a theme only they raised is neither a fix nor a keep
    unscored = [resp(n + 20, what_went_wrong="Slow") for n in range(3)]
    quiet = Theme(theme="Slow site", summary="The site was slow.", n=3, quotes=[], response_ids=[r.id for r in unscored])
    found.questions[0].themes = [quiet]
    none = await recommendations(found, list(QUESTIONS.values()), rs + unscored, RecordingLLM(), "unused")
    assert not none.fix and not none.keep

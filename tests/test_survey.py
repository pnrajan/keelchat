"""The survey step (ported from Verlox): closed answers read in code, conditions on earlier answers, follow-ups on open
answers counted in code, and a model reading a messy reply only after the parser fails, confirmed before it is kept."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from keelchat.capabilities.nodes.survey import SurveyConfig
from keelchat.capabilities.nodes.survey_answers import AskIf, SurveyQuestion, condition_met, describe, parse
from tests.helpers import make_runtime, say

SURVEY = Path(__file__).parents[1] / "examples" / "nps_survey.yaml"


def q(**kw: object) -> SurveyQuestion:
    return SurveyQuestion.model_validate({"key": "q", "prompt": "?", **kw})


@pytest.mark.parametrize(("reply", "value"), [("probably a 7", 7), ("7/10", 7), ("10", 10), ("I'd say 3.6", 4)])
def test_ratings_are_read_generously(reply, value):
    assert parse(q(kind="scale", scale_min=0, scale_max=10), reply).value == value


def test_choices_ranks_and_allocations_never_guess():
    opts = ["Search engine", "Social media", "A friend", "Other"]
    single = q(kind="single_choice", options=opts)
    assert parse(single, "b").value == "social_media" and parse(single, "a friend").value == "a_friend"
    assert parse(single, "s").error  # one letter that isn't an option letter: refuse, don't guess
    rank = q(kind="rank", options=["Price", "Delivery speed", "Quality"], top_n=2)
    assert parse(rank, "quality then price").value == ["quality", "price"]
    assert "top 2" in parse(rank, "quality").error
    alloc = q(kind="allocation", options=["Price", "Speed"], total_points=100)
    assert parse(alloc, "a 70, b 30").value == {"price": 70, "speed": 30}
    assert "adds up to 90" in parse(alloc, "a 60, b 30").error
    assert describe(rank, ["quality", "price"]) == "Quality, then Price"


def test_conditions_are_decided_in_code():
    assert condition_met(AskIf(question="nps", op="lte", value="6"), 4)
    assert not condition_met(AskIf(question="nps", op="lte", value="6"), 9)
    assert not condition_met(AskIf(question="nps", op="answered"), None)  # skipped never meets a condition
    assert condition_met(AskIf(question="ch", op="any_of", values=["a", "b"]), ["b", "c"])


def test_bad_surveys_are_rejected_with_the_reason():
    with pytest.raises(ValidationError, match="closed answers are read"):
        q(kind="scale", expectation="why")
    with pytest.raises(ValidationError, match="not an earlier question"):
        SurveyConfig.model_validate({"questions": [{"key": "a", "prompt": "?", "ask_if": {"question": "b", "op": "answered"}}, {"key": "b", "prompt": "?"}]})
    with pytest.raises(ValidationError, match="has no option"):
        SurveyConfig.model_validate(
            {
                "questions": [
                    {"key": "a", "prompt": "?", "kind": "single_choice", "options": ["x", "y"]},
                    {"key": "b", "prompt": "?", "ask_if": {"question": "a", "value": "z"}},
                ]
            }
        )


async def start(rt):
    s = await rt.start_session("nps-survey", "u1")
    await say(rt, s.id, "hi")
    r = await say(rt, s.id, "yes")
    assert r.state.open_question.field == "nps", r.outbound.text
    return s, r


async def test_a_low_score_is_followed_up_until_the_answer_is_complete():
    rt, _ = await make_runtime(SURVEY.read_text())
    s, r = await start(rt)
    assert "0 to 10" in r.outbound.text
    r = await say(rt, s.id, "honestly a 4")
    assert r.state.fields["nps"] == 4 and r.state.open_question.field == "what_went_wrong", r.outbound.text  # low score → why
    r = await say(rt, s.id, "it was bad")  # too thin: a follow-up, counted in code
    assert r.state.open_question.field == "what_went_wrong" and r.state.data["survey"]["probes"]["what_went_wrong"] == 1, r.outbound.text
    r = await say(rt, s.id, "The parcel arrived a week late and the mug inside was cracked, so I couldn't give it as a birthday gift.")
    if r.state.open_question.field == "what_went_wrong":  # the model may ask once more; the cap is 2
        r = await say(rt, s.id, "That's all, the delay and the damage were the problems.")
    assert "cracked" in r.state.fields["what_went_wrong"] and "best_part" not in r.state.fields or r.state.fields["best_part"] is None
    assert r.state.open_question.field == "channel" and "(a) Search engine" in r.outbound.text, r.outbound.text


async def test_a_messy_choice_is_read_back_before_it_is_kept():
    rt, _ = await make_runtime(SURVEY.read_text())
    s, _ = await start(rt)
    await say(rt, s.id, "9")
    r = await say(rt, s.id, "Fast delivery and the glaze looked great")
    assert r.state.open_question.field == "channel", r.outbound.text
    r = await say(rt, s.id, "my sister told me about you")  # no letter: the model reads it, the parser checks it
    assert r.state.open_question.field == "channel__confirm" and "friend" in r.outbound.text.lower(), r.outbound.text
    r = await say(rt, s.id, "yes")
    assert r.state.fields["channel"] == "a_friend" and r.state.data["survey"]["capture"]["channel"] == "confirmed"
    r = await say(rt, s.id, "quality, then price")
    assert r.state.steps["survey"].status == "complete", r.outbound.text
    out = r.state.outputs["survey"]
    assert out["answers"]["priorities"] == ["quality", "price"] and out["answers"]["what_went_wrong"] is None
    assert out["capture"]["nps"] == "parsed"
    (saved,) = await rt.responses.list("default", "nps-survey")  # kept for findings once the session is gone
    assert saved.session_id == s.id and saved.answers["channel"] == "a_friend" and saved.capture["channel"] == "confirmed"

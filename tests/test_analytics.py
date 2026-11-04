from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd
import pytest
from httpx import ASGITransport, AsyncClient

from yachat.analytics.analyst import Analyst, Limits
from yachat.analytics.api import create_app
from yachat.analytics.duck import AnalyticsError, DatasetDB, check_select, summarize
from yachat.analytics.grounding import Grounding
from yachat.analytics.models import AnalyzeIn, HistoryItem
from yachat.analytics.service import AnalyticsService
from yachat.datasets.models import Dataset, DatasetIn, SqlExample, StructuredDataset, TableSource
from yachat.runtime.llm import ModelConfig
from yachat.runtime.llm_factory import llm_from_env

DATA = Path(__file__).resolve().parent.parent / "examples" / "data"
TOTALS = {"APAC": 14620, "EU": 10450, "US": 30990}


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "data"
    shutil.copytree(DATA, r)
    monkeypatch.setenv("YACHAT_DATA_ROOT", str(r))
    return r


def sales(root: Path, examples: list[SqlExample] | None = None) -> Dataset:
    spec = StructuredDataset(
        tables=[
            TableSource(name="orders", uri=str(root / "orders.csv"), description="One row per order", columns={"amount": "order total in USD"}),
            TableSource(name="targets", uri=str(root / "targets.parquet"), description="Quarterly revenue target per region"),
        ],
        examples=examples or [],
    )
    return Dataset.create("acme", "sales", DatasetIn(description="Online store orders, January to June 2026", spec=spec))


# ---------------------------------------------------------------- DuckDB layer (deterministic)


def test_only_one_select_is_allowed():
    assert check_select("SELECT 1;") == "SELECT 1"
    assert check_select("WITH x AS (SELECT 1 AS a) SELECT a FROM x")
    for bad, kind in (
        ("DROP TABLE orders", "sql_not_select"),
        ("SELECT 1; DROP TABLE orders", "sql_invalid"),
        ("COPY orders TO 'x.csv'", "sql_not_select"),
        ("PRAGMA version", "sql_not_select"),
        ("SELEC 1", "sql_invalid"),
    ):
        with pytest.raises(AnalyticsError) as e:
            check_select(bad)
        assert e.value.kind == kind


async def test_loaded_database_is_locked(root, tmp_path):
    (tmp_path / "secret.csv").write_text("password\nhunter2\n")
    db = DatasetDB(sales(root))
    assert [(t.name, t.rows) for t in db.tables] == [("orders", 576), ("targets", 3)]
    assert "order total in USD" in db.ddl()[0] and db.tables_in("select * from orders") == ["orders"]
    for sql in (
        f"SELECT * FROM read_csv_auto('{tmp_path / 'secret.csv'}')",
        "SELECT * FROM read_text('/etc/hosts')",
        "SELECT current_setting('enable_external_access') AS x, * FROM (SELECT 1) WHERE 1=0 UNION ALL SELECT * FROM read_csv('/etc/passwd')",
    ):
        with pytest.raises(AnalyticsError) as e:
            await db.query(sql)
        assert e.value.kind == "sql_error"
    r = await db.query("SELECT region, sum(amount) AS revenue FROM orders GROUP BY region ORDER BY region")
    assert r.rows == [["APAC", 14620.0], ["EU", 10450.0], ["US", 30990.0]] or r.rows == [["APAC", 14620], ["EU", 10450], ["US", 30990]]


async def test_row_cap_timeout_and_summary(root):
    db = DatasetDB(sales(root))
    r = await db.query("SELECT * FROM orders", row_cap=10, stats_cap=100)
    assert len(r.rows) == 10 and r.truncated and r.row_count == 100 and r.stats_capped
    with pytest.raises(AnalyticsError) as e:
        await db.query("SELECT count(*) FROM range(10000000000) a, range(10) b", timeout_s=0.3)
    assert e.value.kind == "sql_timeout"
    assert (await db.query("SELECT 1 AS ok")).rows == [[1]]  # still usable after an interrupt
    s = summarize(pd.DataFrame({"amount": [1.0, 2.0, None], "region": ["EU", "EU", "US"]}))
    assert s["amount"] == {"count": 2, "nulls": 1, "sum": 3.0, "mean": 1.5, "min": 1.0, "max": 2.0, "std": 0.707107}
    assert s["region"] == {"count": 3, "nulls": 0, "distinct": 2, "top": "EU"}


def test_sources_outside_the_root_never_load(root, tmp_path):
    (tmp_path / "other.csv").write_text("a\n1\n")
    ds = Dataset.create("acme", "x", DatasetIn(spec=StructuredDataset(tables=[TableSource(name="t", uri=str(tmp_path / "other.csv"))])))
    with pytest.raises(ValueError, match="outside YACHAT_DATA_ROOT"):
        DatasetDB(ds)


async def test_small_samples_are_flagged_only_for_raw_rows(root):
    from yachat.analytics.analyst import Analyst
    from yachat.analytics.models import AnalysisData

    db = DatasetDB(sales(root))
    analyst = Analyst(None, ModelConfig())  # type: ignore[arg-type]  # no model call in this check
    for sql, flagged in (("SELECT * FROM orders LIMIT 5", True), ("SELECT region, sum(amount) FROM orders GROUP BY region", False)):
        r = await db.query(sql)
        notes = analyst._system_assumptions(r, AnalysisData(sql=sql, summary=summarize(r.frame)))
        assert any("Small result" in a.text for a in notes) is flagged


def test_group_subtotals_are_grounded():
    from yachat.analytics.analyst import _subtotals

    frame = pd.DataFrame({"product": ["Team", "Pro", "Team", "Pro"], "region": ["US", "US", "EU", "EU"], "revenue": [19200, 8350, 6720, 2850]})
    g = Grounding(_subtotals(frame))
    assert g.grounded(27550) and g.grounded(9570) and g.grounded(4785) and not g.grounded(30990)  # US, EU, EU mean; not invented


def test_grounding():
    g = Grounding([14620.0, 10450.0, 30990.0, 56060.0])
    assert not g.ungrounded("US led with 30,990, 55% of the 56,060 total; EU trailed at 10,450 (-66% vs US).")
    assert g.ungrounded({"forecast": 61000, "note": "EU 10,450"}) == [61000.0]
    assert g.grounded(3) and not g.grounded(4242)


def test_small_statistics_are_grounded_only_within_their_rounding():
    monthly = [3230.0, 2600.0, 1360.0, 1310.0, 1040.0, 910.0]
    stats = [-466.57142857142856, 0.008578312382718507, 0.8526533550829174, 6.0]
    g = Grounding([*monthly, *stats], pair_base=[*monthly, *stats])
    assert all(g.grounded(v) for v in (0.0086, 0.009, 0.01, -466.6, 0.85, 71.8))  # roundings; a percentage change
    assert not any(g.grounded(v) for v in (0.017, 0.02, 0.05, 0.0043))  # a doubled or halved p-value is not the data


# ---------------------------------------------------------------- the Analyst (real model)


@pytest.fixture
def service():
    return AnalyticsService(Analyst(llm_from_env(), ModelConfig.from_env(), Limits()))


def _numbers(result) -> str:
    return " ".join([result.summary, *[f.statement + " " + str(f.values) for f in result.outcome.findings]]).replace(",", "")


async def test_analyst_answers_with_grounded_findings_and_recommendations(root, service):
    r = await service.analyze(AnalyzeIn(question="Which region brings in the most revenue, and how do the regions compare?", dataset=sales(root)))
    assert r.status in ("ok", "partial") and r.error_kind is None, r
    assert "orders" in r.data.tables and "sum" in r.data.sql.lower() and r.data.row_count >= 3
    assert "30990" in _numbers(r) and r.outcome.findings and r.outcome.attempts["sql"] >= 1
    assert all(set(rec.based_on) <= {f.id for f in r.outcome.findings} for rec in r.recommendations)
    assert r.input.dataset == "sales" and r.input.dataset_version == sales(root).version
    assert not any("Small result" in a.text for a in r.assumptions)  # 3 aggregated rows are not a small sample


async def test_follow_up_resolves_against_history(root, service):
    first = "What is the total revenue by region?"
    sql = "SELECT region, sum(amount) AS revenue FROM orders GROUP BY region"
    r = await service.analyze(AnalyzeIn(question="now only for the Pro product", dataset=sales(root), history=[HistoryItem(question=first, sql=sql)]))
    assert r.status != "failed", r
    assert "pro" in r.input.resolved_question.lower() and "region" in r.input.resolved_question.lower()
    assert "'pro'" in r.data.sql.lower() and "region" in r.data.sql.lower()


async def test_no_matching_rows_is_partial(root, service):
    r = await service.analyze(AnalyzeIn(question="List the orders placed in the region 'Antarctica'.", dataset=sales(root)))
    assert r.status == "partial" and r.error_kind == "no_rows" and not r.recommendations


async def test_sql_errors_are_repaired_within_budget(root, service, monkeypatch):
    calls: list[str] = []
    real = DatasetDB.query

    async def first_fails(self, sql, **kw):  # the database rejects the first query; the analyst must repair it
        calls.append(sql)
        if len(calls) == 1:
            # a syntax error, not a made-up missing column: the model must repair the query, not doubt the schema
            raise AnalyticsError("sql_error", 'Parser Error: syntax error at or near "FROM"')
        return await real(self, sql, **kw)

    monkeypatch.setattr(DatasetDB, "query", first_fails)
    r = await service.analyze(AnalyzeIn(question="Total revenue across all orders?", dataset=sales(root), repairs={"sql": 2, "verify": 0}))
    assert r.outcome.attempts["sql"] == 2 and r.status != "failed" and "56060" in _numbers(r)
    calls.clear()
    monkeypatch.setattr(DatasetDB, "query", lambda *a, **k: (_ for _ in ()).throw(AnalyticsError("sql_error", "x")))
    r = await service.analyze(AnalyzeIn(question="Total revenue?", dataset=sales(root), repairs={"sql": 0}))
    assert r.status == "failed" and r.error_kind == "sql_error" and r.outcome.attempts == {"sql": 1}


async def test_answers_are_planned_and_verified_before_they_are_returned(root, service):
    r = await service.analyze(AnalyzeIn(question="What is the average order amount in each region?", dataset=sales(root)))
    v = r.verification
    assert v is not None and v.plan.method and v.plan.rationale and v.checks, r
    assert not any(c.code for c in v.checks)  # no sandbox: every check is judged by the verifier
    assert {x.id for x in v.results} == {c.id for c in v.checks} and r.outcome.attempts["verify"] == v.rounds <= 3
    if v.passed:
        assert not v.issues and all(x.passed for x in v.results)
    else:  # never presented as verified
        assert r.status == "partial" and any("did not pass verification" in x for x in r.outcome.limitations)


async def test_a_wrong_answer_is_sent_back_by_the_verifier(root, service, monkeypatch):
    real = Analyst._produce
    feedback_seen = []

    async def first_is_wrong(self, task, plan, checks, feedback):  # the first answer claims EU leads; the data says US
        out = await real(self, task, plan, checks, feedback)
        feedback_seen.append(list(feedback))
        if len(feedback_seen) == 1:
            out.summary = "EU brought in the most revenue of any region, well ahead of the US and APAC."
        return out

    monkeypatch.setattr(Analyst, "_produce", first_is_wrong)
    r = await service.analyze(AnalyzeIn(question="Which region brought in the most revenue?", dataset=sales(root)))
    v = r.verification
    assert v is not None and v.rounds >= 2 and not feedback_seen[0] and feedback_seen[1], v  # the problems reached round 2
    assert "EU brought in the most" not in r.summary and "30990" in _numbers(r)


async def test_api(root, service):
    async with AsyncClient(transport=ASGITransport(app=create_app(service)), base_url="http://t") as c:
        ds = sales(root).model_dump(mode="json")
        r = await c.post("/datasets", json=ds)
        assert r.status_code == 200 and r.json()["tables"][0] == {"name": "orders", "rows": 576, "columns": r.json()["tables"][0]["columns"]}
        bad = dict(ds, spec={"kind": "structured", "tables": [{"name": "t", "uri": "/etc/passwd", "format": "csv"}]}, name="bad")
        r = await c.post("/datasets", json=bad)
        assert r.status_code == 422 and "outside YACHAT_DATA_ROOT" in r.text  # the caller's own URI, never file content
        r = await c.post("/analyze", json={"question": "How many orders are there?", "dataset": ds})
        assert r.status_code == 200 and r.json()["schema_version"] == 1 and "576" in r.text
        calls = r.json()["usage"]  # the service's own model calls, for the caller's cost page
        assert calls and all(c["model"] and c["purpose"] and c["input_tokens"] > 0 and c["output_tokens"] > 0 for c in calls)


async def test_a_folder_of_parquet_files_is_one_table(root, tmp_path):
    parts = root / "orders_parquet"
    for i, chunk in enumerate(pd.read_csv(root / "orders.csv").pipe(lambda d: [d.iloc[:300], d.iloc[300:]])):
        (parts / f"month={i}").mkdir(parents=True)
        chunk.to_parquet(parts / f"month={i}" / "part.parquet", index=False)
    spec = StructuredDataset(tables=[TableSource(name="orders", uri=str(parts), format="parquet")])
    db = DatasetDB(Dataset.create("acme", "p", DatasetIn(spec=spec)))
    assert db.tables[0].rows == 576 and (await db.query("SELECT sum(amount) AS s FROM orders")).rows == [[56060]]
    with pytest.raises(ValueError, match="cannot infer the format"):
        TableSource(name="orders", uri=str(parts))  # a folder has no suffix: the format is required
    empty = root / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="has no .parquet files"):
        DatasetDB(Dataset.create("acme", "e", DatasetIn(spec=StructuredDataset(tables=[TableSource(name="t", uri=str(empty), format="parquet")]))))


def test_only_statistics_reach_the_answer():
    from yachat.analytics.analyst import statistics_of

    got = statistics_of(
        {
            "slope": -466.57,
            "p_value": 0.0086,
            "n": 6,
            "method": "OLS",
            "significant": True,
            "sql_query": "SELECT " + "x, " * 100,
            "monthly_data": [{"m": 1}],
            "table": {"a": 1},
        }
    )
    assert got == {"slope": -466.57, "p_value": 0.0086, "n": 6, "method": "OLS", "significant": True}

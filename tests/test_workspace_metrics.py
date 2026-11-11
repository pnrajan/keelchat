"""Key metrics as SQL over a workspace's audit records, and plain-English questions answered with text to SQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

pytest.importorskip("duckdb")

from keelchat.analytics.duck import AnalyticsError, DatasetDB  # noqa: E402
from keelchat.analytics.workspace_metrics import ask, key_metrics, metrics_db  # noqa: E402
from keelchat.runtime.llm import ModelConfig  # noqa: E402
from keelchat.runtime.llm_factory import llm_from_env  # noqa: E402
from keelchat.state.models import AuditEvent  # noqa: E402
from tests.helpers import audit_window  # noqa: E402

T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(days=1)


def ev(sid: str, kind: str, decision: str, minutes: float = 0, agent: str = "support", **kw: object) -> AuditEvent:
    at = T0 + timedelta(minutes=minutes)
    return AuditEvent.model_validate({"tenant": "acme", "agent": agent, "session_id": sid, "kind": kind, "decision": decision, "at": at, **kw})


def workspace() -> list[AuditEvent]:
    return [
        # s1: completed in 3 turns, one rejected answer
        ev("s1", "turn", "form_fill", 0, ms=1000.0, llm_calls=2),
        ev("s1", "answer", "rejected", 1, field="address"),
        ev("s1", "turn", "form_fill", 1, ms=2000.0, llm_calls=2),
        ev("s1", "turn", "form_fill", 2, ms=3000.0, llm_calls=2, errors=1),
        ev("s1", "exit", "completed", 2),
        ev("s1", "llm", "call", 0, ref="m", reason="render", input_tokens=100, output_tokens=10, cost_usd=0.002),
        # s2: handed off, claimed after 30 minutes
        ev("s2", "turn", "handoff", 5, ms=4000.0, llm_calls=1, agent="hiring"),
        ev("s2", "handoff", "opened", 5, ref="hof_1", reason="user_request", agent="hiring"),
        ev("s2", "exit", "handed_off", 5, agent="hiring"),
        ev("s2", "handoff", "claimed", 35, ref="hof_1", actor="ana@team", agent="hiring"),
        ev("s2", "llm", "call", 5, ref="m", reason="intent_detect", input_tokens=50, output_tokens=5, cost_usd=0.001, agent="hiring"),
        # s3: still open
        ev("s3", "turn", "greet_consent", 10, ms=500.0, llm_calls=1),
        # s4: dropped
        ev("s4", "turn", "form_fill", 20, ms=800.0, llm_calls=1),
        ev("s4", "exit", "max_reasks", 20),
    ]


async def tables(events: list[AuditEvent]) -> DatasetDB:
    return metrics_db(await audit_window(events, "acme"))


async def test_key_metrics_are_computed_from_the_audit_records():
    db = await tables([*workspace(), *(e.model_copy(update={"tenant": "globex"}) for e in workspace())])
    got = {v.metric.name: v.value for v in await key_metrics(db)}
    assert got["conversations"] == 4
    assert got["completion_rate"] == 0.25 and got["handoff_rate"] == 0.25 and got["drop_rate"] == 0.25
    assert got["turns_per_conversation"] == 1.5
    assert got["p95_reply_ms"] == 4000.0 and got["error_turn_rate"] == pytest.approx(1 / 6, abs=1e-5)
    assert got["total_cost"] == pytest.approx(0.003) and got["cost_per_conversation"] == pytest.approx(0.00075)
    assert got["minutes_to_claim"] == 30.0


async def test_an_empty_workspace_has_no_values_and_no_errors():
    got = {v.metric.name: v.value for v in await key_metrics(await tables([]))}
    assert got["conversations"] == 0 and got["completion_rate"] is None and got["p95_reply_ms"] is None


async def test_the_database_only_reads_its_own_tables():
    db = await tables(workspace())
    for sql in ("SELECT * FROM read_csv_auto('/etc/passwd')", "DROP TABLE conversations", "SELECT 1; SELECT 2"):
        with pytest.raises(AnalyticsError):
            await db.query(sql)


async def test_a_question_in_plain_english_is_answered_with_sql():
    db = await tables(workspace())
    a = await ask(db, llm_from_env(), ModelConfig.from_env().generate, "Which agent handed the most conversations to a person?")
    assert "conversations" in a.sql.lower() or "handoffs" in a.sql.lower()
    assert a.rows and a.rows[0][0] == "hiring", (a.sql, a.rows)


async def test_each_workspace_queries_only_its_own_records():
    from httpx import ASGITransport, AsyncClient

    from keelchat.api.app import create_app
    from keelchat.runtime.runtime import Runtime
    from keelchat.state.store import InMemoryAuditStore, InMemoryTenantToolStore

    audit = InMemoryAuditStore()
    await audit.append(workspace())
    await audit.append([e.model_copy(update={"tenant": "globex", "agent": "globex-secret", "session_id": f"g{i}"}) for i, e in enumerate(workspace())])
    rt = Runtime(llm_from_env(), {}, models=ModelConfig.from_env(), audit=audit)
    app = create_app(rt, tool_store=InMemoryTenantToolStore())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        m = (await c.get("/v1/tenants/acme/metrics", params={"days": 7})).json()
        assert {x["name"]: x["value"] for x in m["metrics"]}["conversations"] == 4
        assert {t["name"] for t in m["tables"]} == {"conversations", "turns", "model_calls", "handoffs", "decisions"}
        r = await c.post("/v1/tenants/acme/metrics/ask", json={"question": "List every agent name that appears in the data", "days": 7})
        assert r.status_code == 200, r.text
        names = {row[0] for row in r.json()["rows"]}
        assert "globex-secret" not in names and names <= {"support", "hiring"}, r.json()
    usage = [e for e in await audit.query("acme", kinds=["llm"]) if e.agent == "workspace"]
    assert usage and usage[0].reason == "analytics_sql"  # the question's model calls are on the cost page

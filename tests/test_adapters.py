from __future__ import annotations

import os

import pytest

from tests.helpers import make_runtime, say
from yachat.state.models import SessionState
from yachat.state.redis_store import RedisSessionStore
from yachat.state.store import VersionConflict


@pytest.fixture
async def redis_client():
    fakeredis = pytest.importorskip("fakeredis")
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


async def test_redis_session_store(redis_client):
    store = RedisSessionStore(redis_client)
    st = SessionState(user_id="u", agent="a", policy_hash="h", channel="web")
    s1 = await store.save(st)
    assert s1.version == 1 and (await store.load(st.id)).version == 1
    with pytest.raises(VersionConflict):
        await store.save(st)
    await store.mark_seen(st.id, "m", {"turn_id": "t"})
    assert (await store.seen(st.id, "m"))["turn_id"] == "t"


async def test_runtime_on_redis(redis_client, policy_yaml):
    rt, _ = await make_runtime(policy_yaml, sessions=RedisSessionStore(redis_client))
    s = await rt.start_session("hiring-agent", "u1")
    await say(rt, s.id, "hello", mid="a")
    r = await say(rt, s.id, "yes", mid="b")
    assert r.state.steps["greet_consent"].status == "complete" and (await say(rt, s.id, "yes", mid="b")).duplicate


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_memory_store():
    import asyncpg

    from yachat.state.models import MemoryRecord
    from yachat.state.postgres_store import PostgresMemoryStore, ensure_schema

    try:
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], timeout=3)
    except Exception as e:  # database down or missing: environment, not code
        pytest.skip(f"postgres unreachable: {e.__class__.__name__}")
    await ensure_schema(pool)
    store = PostgresMemoryStore(pool)
    await store.upsert(MemoryRecord(user_id="pgu", agent="a", item="location", value="Austin", half_life_s=100.0))
    assert (await store.list("pgu", "a"))[0].value == "Austin"
    assert await store.delete_user("pgu") == 1
    await pool.close()


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_memory_is_isolated_per_tenant():
    import asyncpg

    from yachat.state.models import MemoryRecord
    from yachat.state.postgres_store import PostgresMemoryStore, ensure_schema

    try:
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], timeout=3)
    except Exception as e:
        pytest.skip(f"postgres unreachable: {e.__class__.__name__}")
    await ensure_schema(pool)
    store = PostgresMemoryStore(pool)
    for t, city in (("acme", "Austin"), ("globex", "Boston")):  # same user id, agent and item in two tenants
        await store.upsert(MemoryRecord(user_id="shared-u", agent="a", item="location", value=city, half_life_s=100.0, tenant=t))
    assert [r.value for r in await store.list("shared-u", "a", "acme")] == ["Austin"]
    assert [r.value for r in await store.list("shared-u", "a", "globex")] == ["Boston"]
    assert await store.delete_user("shared-u", tenant="acme") == 1
    assert [r.value for r in await store.list("shared-u", "a", "globex")] == ["Boston"]  # the other tenant is untouched
    await store.delete_user("shared-u", tenant="globex")
    await pool.close()


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_tenant_tool_store():
    import uuid

    import asyncpg

    from yachat.state.postgres_store import PostgresTenantToolStore, ensure_schema

    try:
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], timeout=3)
    except Exception as e:
        pytest.skip(f"postgres unreachable: {e.__class__.__name__}")
    await ensure_schema(pool)
    store, tenant = PostgresTenantToolStore(pool), "t-" + uuid.uuid4().hex[:8]
    reg = {"name": "crm", "description": "x", "url": "https://crm.example.com", "input_schema": {"type": "object"}}
    assert await store.add(tenant, "crm", reg) is True
    assert await store.add(tenant, "crm", {**reg, "url": "https://evil.example.com"}) is False  # immutable
    assert await store.list(tenant) == [(tenant, reg)]
    assert (tenant, reg) in await store.list()
    async with pool.acquire() as con:
        await con.execute("DELETE FROM tenant_tools WHERE tenant=$1", tenant)
    await pool.close()


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_audit_between_reads_every_tenant_oldest_first():
    import uuid
    from datetime import UTC, datetime, timedelta

    import asyncpg

    from yachat.state.models import AuditEvent
    from yachat.state.postgres_store import PostgresAuditStore, ensure_schema

    try:
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], timeout=3)
    except Exception as e:  # database down or missing: environment, not code
        pytest.skip(f"postgres unreachable: {e.__class__.__name__}")
    await ensure_schema(pool)
    store, tag = PostgresAuditStore(pool), uuid.uuid4().hex[:8]
    t0 = datetime(2001, 1, 1, tzinfo=UTC)
    events = [AuditEvent(tenant=f"{tag}-{i % 2}", agent="a", kind="turn", decision="ask", at=t0 + timedelta(minutes=i)) for i in range(4)]
    await store.append(list(reversed(events)))
    got = await store.between(t0 + timedelta(minutes=1), t0 + timedelta(minutes=3))
    assert [e.id for e in got] == [events[1].id, events[2].id]
    async with pool.acquire() as con:
        await con.execute("DELETE FROM audit_events WHERE tenant LIKE $1", f"{tag}-%")
    await pool.close()


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_audit_copies_a_tenants_window_into_duckdb():
    import uuid
    from datetime import UTC, datetime, timedelta

    import asyncpg

    from yachat.runtime.audit import audit_db
    from yachat.state.models import AuditEvent
    from yachat.state.postgres_store import PostgresAuditStore, ensure_schema

    try:
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], timeout=3)
    except Exception as e:  # database down or missing: environment, not code
        pytest.skip(f"postgres unreachable: {e.__class__.__name__}")
    await ensure_schema(pool)
    store, tenant = PostgresAuditStore(pool), "t-" + uuid.uuid4().hex[:8]
    t0 = datetime(2001, 1, 1, tzinfo=UTC)
    call = AuditEvent(tenant=tenant, agent="a", kind="llm", decision="call", reason="", ref="m", input_tokens=7, cost_usd=0.5, ms=1.5, at=t0)
    turn = AuditEvent(tenant=tenant, agent="a", session_id="s1", kind="turn", decision="ask", ok=False, text="hi, it's me", at=t0 + timedelta(hours=1))
    await store.append(
        [
            call,
            turn,
            call.model_copy(update={"id": "x" + tenant, "tenant": tenant + "x"}),
            call.model_copy(update={"id": "y" + tenant, "at": t0 - timedelta(days=1)}),
        ]
    )
    db = await audit_db(store, tenant, t0)
    try:
        rows = db.execute('SELECT id, "at", kind, reason, ref, ok, input_tokens, cost_usd, ms, saved_tokens FROM audit ORDER BY "at"').fetchall()
        assert rows == [(call.id, t0, "llm", "", "m", None, 7, 0.5, 1.5, None), (turn.id, turn.at, "turn", "", None, False, None, None, None, None)]
    finally:
        db.close()
        async with pool.acquire() as con:
            await con.execute("DELETE FROM audit_events WHERE tenant LIKE $1", f"{tenant}%")
        await pool.close()


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_every_postgres_store_shares_one_pool():
    from yachat.api.app import embed_store_from_env, key_store_from_env, stores_from_env, tool_store_from_env

    try:
        stores = [*stores_from_env().values(), embed_store_from_env(), key_store_from_env(), tool_store_from_env()]
        pools = {id(await s.pool) for s in stores if hasattr(s, "pool")}
    except OSError as e:  # database down: environment, not code
        pytest.skip(f"postgres unreachable: {e.__class__.__name__}")
    assert len(pools) == 1 and len(stores) >= 10


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_session_store_versions_dedupes_and_survives_a_restart(policy_yaml):
    from yachat.state.postgres_store import PostgresSessionStore

    url = os.environ["DATABASE_URL"]
    store = PostgresSessionStore(url=url)
    st = SessionState(user_id=f"u{os.urandom(3).hex()}", agent="a", policy_hash="h", channel="web")
    s1 = await store.save(st)
    assert s1.version == 1 and (await store.load(st.id)).version == 1  # type: ignore[union-attr]
    with pytest.raises(VersionConflict):  # a second writer from the same version loses
        await store.save(st)
    assert (await store.find_open(st.user_id, "a")).id == st.id  # type: ignore[union-attr]
    await store.mark_seen(st.id, "m", {"turn_id": "t"})
    await store.mark_seen(st.id, "m", {"turn_id": "other"})  # the first result stands
    assert (await store.seen(st.id, "m"))["turn_id"] == "t"  # type: ignore[index]

    # a conversation carries on after a restart: a new runtime on the same database picks it up
    rt, _ = await make_runtime(policy_yaml, sessions=PostgresSessionStore(url=url))
    s = await rt.start_session("hiring-agent", st.user_id)
    await say(rt, s.id, "hello", user=st.user_id)
    rt2, _ = await make_runtime(policy_yaml, sessions=PostgresSessionStore(url=url))
    r = await say(rt2, s.id, "yes", user=st.user_id)
    assert r.state.steps["greet_consent"].status == "complete"


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_job_store_round_trips_and_is_tenant_scoped():
    from yachat.state.postgres_store import PostgresJobStore
    from yachat.state.store import InsightJob

    store = PostgresJobStore(url=os.environ["DATABASE_URL"])
    job = InsightJob(tenant=f"t{os.urandom(3).hex()}", request={"days": 7})
    await store.put(job)
    job.status, job.result = "done", {"agents": ["a"]}
    await store.put(job)
    got = await store.get(job.tenant, job.id)
    assert got is not None and got.status == "done" and got.result == {"agents": ["a"]}
    assert await store.get("someone-else", job.id) is None


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_rate_limits_are_shared_by_every_server():
    from yachat.state.postgres_store import PostgresRateLimiter

    url, key = os.environ["DATABASE_URL"], f"k{os.urandom(3).hex()}"
    a, b = PostgresRateLimiter(url=url), PostgresRateLimiter(url=url)  # two servers, one database
    assert [await a.take(key, 3, 60), await b.take(key, 3, 60), await a.take(key, 3, 60)] == [True, True, True]
    assert not await b.take(key, 3, 60) and not await a.take(key, 3, 60)
    assert await a.take(f"{key}-other", 3, 60)  # keys are counted apart


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_turn_locks_let_one_server_at_a_time_hold_a_chat():
    import asyncio

    from yachat.state.postgres_store import PostgresTurnLocks

    url, key = os.environ["DATABASE_URL"], f"s{os.urandom(3).hex()}"
    a, b = PostgresTurnLocks(url=url), PostgresTurnLocks(url=url)
    order: list[str] = []

    async def hold(locks: PostgresTurnLocks, name: str) -> None:
        async with locks.hold(key):
            order.append(f"{name}+")
            await asyncio.sleep(0.3)
            order.append(f"{name}-")

    await asyncio.gather(hold(a, "a"), hold(b, "b"))
    assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])  # never interleaved


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_a_lock_left_by_a_crashed_server_expires():
    from yachat.state.postgres_store import PostgresTurnLocks

    url, key = os.environ["DATABASE_URL"], f"s{os.urandom(3).hex()}"
    crashed = PostgresTurnLocks(url=url, lease_s=1)
    assert await crashed.acquire(key, "dead-holder")  # taken and never released
    async with PostgresTurnLocks(url=url).hold(key):  # another server gets it once the lease runs out
        pass


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_usage_starts_again_each_month(monkeypatch):
    from yachat.state import store
    from yachat.state.postgres_store import PostgresBudgetStore

    budgets, tenant = PostgresBudgetStore(url=os.environ["DATABASE_URL"]), f"t{os.urandom(3).hex()}"
    monkeypatch.setattr(store, "usage_period", lambda: "2026-11")
    await budgets.set_plan(tenant, limit_turns=100, limit_usd=0.25, small_models=True)
    await budgets.charge(tenant, 0.25, turns=100)
    assert (await budgets.get(tenant)).exhausted()
    monkeypatch.setattr(store, "usage_period", lambda: "2026-12")
    plan = await budgets.get(tenant)
    assert plan.turns == 0 and plan.spent_usd == 0 and not plan.exhausted()
    await budgets.charge(tenant, 0.01, turns=1)
    await budgets.charge(tenant, 0.01, turns=1)
    assert (await budgets.get(tenant)).turns == 2


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_review_store() -> None:
    import asyncpg

    from yachat.state.models import Review, ReviewTurn
    from yachat.state.postgres_store import PostgresReviewStore, ensure_schema

    try:
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], timeout=3)
    except Exception as e:  # database down or missing: environment, not code
        pytest.skip(f"postgres unreachable: {e.__class__.__name__}")
    await ensure_schema(pool)
    store = PostgresReviewStore(pool)
    r = Review(tenant="pg-t", agent="pg-a", source="chat", conversation="c", turns=[ReviewTurn(user="hi", agent="hello")], score=3)
    await store.save(r)
    await store.save(r.model_copy(update={"score": 4}))
    (got,) = await store.list("pg-t", "pg-a")
    assert got.id == r.id and got.score == 4 and await store.list("pg-t", "other") == []
    assert (await store.get("pg-t", r.id)) == got and await store.get("pg-t", "rev_missing") is None
    await store.save(r.model_copy(update={"agent": "pg-b", "score": 1}))  # an id of another agent's rating changes nothing
    assert (await store.get("pg-t", r.id)).agent == "pg-a" and await store.list("pg-t", "pg-b") == []
    await pool.close()


@pytest.mark.skipif(not os.getenv("DATABASE_URL"), reason="DATABASE_URL not set")
async def test_postgres_agent_test_store() -> None:
    import asyncpg

    from yachat.evals.case import Expect, ReplayCase
    from yachat.state.models import AgentTest
    from yachat.state.postgres_store import PostgresAgentTestStore, ensure_schema

    try:
        pool = await asyncpg.create_pool(os.environ["DATABASE_URL"], timeout=3)
    except Exception as e:  # database down or missing: environment, not code
        pytest.skip(f"postgres unreachable: {e.__class__.__name__}")
    await ensure_schema(pool)
    store = PostgresAgentTestStore(pool)
    t = AgentTest(tenant="pg-t", agent="pg-a", case=ReplayCase(name="done", messages=["hi", "yes"], expect=Expect(completed=True)))
    await store.save(t)
    await store.save(t.model_copy(update={"source": "chat"}))
    (got,) = await store.list("pg-t", "pg-a")
    assert got == t.model_copy(update={"source": "chat"}) and got.case.expect.completed is True
    await store.save(t.model_copy(update={"agent": "pg-b"}))  # an id of another agent's test changes nothing
    assert (await store.get("pg-t", t.id)).agent == "pg-a" and await store.list("pg-t", "pg-b") == []
    assert await store.delete("pg-t", t.id) and not await store.delete("pg-t", t.id) and await store.list("pg-t", "pg-a") == []
    await pool.close()

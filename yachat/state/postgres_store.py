"""Postgres adapters (asyncpg): memory (one row per tenant, user, agent and item), tenant tools, datasets, saved
policy versions, the audit log and handoffs."""

from __future__ import annotations

import asyncio
import builtins
import json
import os
import tempfile
from datetime import datetime
from typing import TYPE_CHECKING, Any

from yachat.api.keys import ApiKey, ApiKeyStore
from yachat.datasets.models import Dataset
from yachat.datasets.store import DatasetStore
from yachat.state import store
from yachat.state.models import (
    AgentSummary,
    AuditEvent,
    AuditKind,
    Embed,
    Handoff,
    HandoffLine,
    HandoffStatus,
    MemoryRecord,
    PolicyTemplate,
    PolicyVersion,
    SessionState,
    SurveyResponse,
    now,
)
from yachat.state.store import (
    AUDIT_COLUMNS,
    AgentVersionStore,
    AuditStore,
    BudgetStore,
    EmbedStore,
    HandoffStore,
    InsightJob,
    JobStore,
    MemoryStore,
    Plan,
    RateLimiter,
    ResponseStore,
    SessionStore,
    TemplateStore,
    TenantToolStore,
    TurnLocks,
    VersionConflict,
)

if TYPE_CHECKING:
    from duckdb import DuckDBPyConnection

try:  # optional extra
    import asyncpg
except ImportError:  # pragma: no cover
    asyncpg = None

DDL = """
CREATE TABLE IF NOT EXISTS user_memory (
    tenant TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    agent TEXT NOT NULL,
    item TEXT NOT NULL,
    value JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    source_turn_id TEXT,
    half_life_s DOUBLE PRECISION NOT NULL,
    sensitive BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (tenant, user_id, agent, item)
);
ALTER TABLE user_memory ADD COLUMN IF NOT EXISTS tenant TEXT NOT NULL DEFAULT 'default';
CREATE TABLE IF NOT EXISTS tenant_tools (
    tenant TEXT NOT NULL,
    name TEXT NOT NULL,
    registration JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, name)
);
CREATE TABLE IF NOT EXISTS datasets (
    tenant TEXT NOT NULL,
    name TEXT NOT NULL,
    dataset JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, name)
);
CREATE TABLE IF NOT EXISTS agent_versions (
    tenant TEXT NOT NULL,
    agent TEXT NOT NULL,
    version INTEGER NOT NULL,
    hash TEXT NOT NULL,
    yaml TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, agent, version),
    UNIQUE (tenant, agent, hash)
);
CREATE TABLE IF NOT EXISTS agent_active (
    tenant TEXT NOT NULL,
    agent TEXT NOT NULL,
    version INTEGER NOT NULL,
    PRIMARY KEY (tenant, agent),
    FOREIGN KEY (tenant, agent, version) REFERENCES agent_versions (tenant, agent, version)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    name TEXT NOT NULL,
    scopes TEXT[] NOT NULL,
    hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS api_keys_tenant ON api_keys (tenant);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    agent TEXT NOT NULL,
    user_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    finished BOOLEAN NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    state JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions (tenant, agent, user_id, updated_at DESC) WHERE NOT finished;
CREATE INDEX IF NOT EXISTS sessions_expires ON sessions (expires_at);
CREATE TABLE IF NOT EXISTS session_seen (
    session_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    result JSONB NOT NULL,
    PRIMARY KEY (session_id, message_id)
);
CREATE TABLE IF NOT EXISTS rate_hits (
    key TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS rate_hits_key ON rate_hits (key, at);
CREATE INDEX IF NOT EXISTS rate_hits_expires ON rate_hits (expires_at);
CREATE TABLE IF NOT EXISTS turn_locks (
    key TEXT PRIMARY KEY,
    holder TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS insight_jobs (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    job JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS insight_jobs_created ON insight_jobs (created_at);
CREATE TABLE IF NOT EXISTS tenant_budgets (
    tenant TEXT PRIMARY KEY,
    limit_usd DOUBLE PRECISION,
    spent_usd DOUBLE PRECISION NOT NULL DEFAULT 0
);
ALTER TABLE tenant_budgets ALTER COLUMN limit_usd DROP NOT NULL;
ALTER TABLE tenant_budgets ADD COLUMN IF NOT EXISTS limit_turns INTEGER;
ALTER TABLE tenant_budgets ADD COLUMN IF NOT EXISTS turns INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tenant_budgets ADD COLUMN IF NOT EXISTS small_models BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tenant_budgets ADD COLUMN IF NOT EXISTS paid BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tenant_budgets ADD COLUMN IF NOT EXISTS period TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS policy_templates (
    tenant TEXT NOT NULL,
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    yaml TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, id)
);
CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    agent TEXT NOT NULL,
    session_id TEXT,
    kind TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL,
    event JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_events_tenant_at ON audit_events (tenant, at DESC);
CREATE INDEX IF NOT EXISTS audit_events_session ON audit_events (tenant, session_id);
CREATE INDEX IF NOT EXISTS audit_events_at ON audit_events (at);
CREATE TABLE IF NOT EXISTS survey_responses (
    session_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    agent TEXT NOT NULL,
    user_id TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL,
    response JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS survey_responses_agent ON survey_responses (tenant, agent, completed_at);
CREATE INDEX IF NOT EXISTS survey_responses_tenant ON survey_responses (tenant, completed_at);
CREATE TABLE IF NOT EXISTS embeds (
    id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    embed JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS embeds_tenant ON embeds (tenant, created_at DESC);
CREATE TABLE IF NOT EXISTS handoffs (
    tenant TEXT NOT NULL,
    id TEXT NOT NULL,
    agent TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    handoff JSONB NOT NULL,
    PRIMARY KEY (tenant, id)
);
CREATE INDEX IF NOT EXISTS handoffs_tenant_status ON handoffs (tenant, status, created_at DESC);
-- tables created before tenancy keyed rows without the tenant, so two tenants overwrote each other: re-key them.
-- The old key was unique on (user_id, agent, item), so the wider key cannot conflict.
DO $$
DECLARE pk record;
BEGIN
    SELECT conname, pg_get_constraintdef(oid) AS def INTO pk
    FROM pg_constraint WHERE conrelid = 'user_memory'::regclass AND contype = 'p';
    IF pk.def IS DISTINCT FROM 'PRIMARY KEY (tenant, user_id, agent, item)' THEN
        IF pk.conname IS NOT NULL THEN
            EXECUTE format('ALTER TABLE user_memory DROP CONSTRAINT %I', pk.conname);
        END IF;
        ALTER TABLE user_memory ADD PRIMARY KEY (tenant, user_id, agent, item);
    END IF;
END $$;
"""


async def ensure_schema(pool: Any) -> None:
    async with pool.acquire() as con:
        await con.execute(DDL)


#: one asyncpg pool per database and event loop, shared by every store (each store opening its own would exhaust
#: Postgres' connection limit: about a dozen stores times asyncpg's default 10 connections)
_POOLS: dict[tuple[str, int], Any] = {}
_POOL_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


async def shared_pool(url: str) -> Any:
    """The process's pool for `url` (DATABASE_POOL_MIN / DATABASE_POOL_MAX connections, default 1 / 10), created and
    given its schema on first use."""
    key = (url, id(asyncio.get_running_loop()))
    async with _POOL_LOCKS.setdefault(key, asyncio.Lock()):
        if key not in _POOLS:
            pool = await asyncpg.create_pool(url, min_size=int(os.getenv("DATABASE_POOL_MIN") or 1), max_size=int(os.getenv("DATABASE_POOL_MAX") or 10))
            await ensure_schema(pool)
            _POOLS[key] = pool
        return _POOLS[key]


class _Pooled:
    """A store's connections: a pool it was given, or the shared pool for its url (created on first use, so a store can
    be built from DATABASE_URL at import time)."""

    def __init__(self, pool: Any = None, url: str | None = None):
        if asyncpg is None:  # pragma: no cover
            raise RuntimeError(f"install yachat[postgres] to use {type(self).__name__}")
        if pool is None and not url:
            raise ValueError(f"{type(self).__name__} needs a pool or a url")
        self._pool, self._url = pool, url

    @property
    async def pool(self) -> Any:
        if self._pool is None:
            assert self._url is not None
            self._pool = await shared_pool(self._url)
        return self._pool


class PostgresMemoryStore(_Pooled, MemoryStore):
    async def upsert(self, r: MemoryRecord) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO user_memory (user_id, agent, item, value, created_at, updated_at, source_turn_id, half_life_s, sensitive, tenant) "
                "VALUES ($1,$2,$3,$4::jsonb,$5,$6,$7,$8,$9,$10) ON CONFLICT (tenant, user_id, agent, item) DO UPDATE SET value=EXCLUDED.value, "
                "updated_at=EXCLUDED.updated_at, source_turn_id=EXCLUDED.source_turn_id, "
                "half_life_s=EXCLUDED.half_life_s, sensitive=EXCLUDED.sensitive",
                r.user_id,
                r.agent,
                r.item,
                json.dumps(r.value),
                r.created_at,
                r.updated_at,
                r.source_turn_id,
                r.half_life_s,
                r.sensitive,
                r.tenant,
            )

    async def list(self, user_id: str, agent: str, tenant: str = "default") -> list[MemoryRecord]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch("SELECT * FROM user_memory WHERE user_id=$1 AND agent=$2 AND tenant=$3", user_id, agent, tenant)
        out = []
        for row in rows:
            d = dict(row)
            d["value"] = json.loads(d["value"]) if isinstance(d["value"], str) else d["value"]
            out.append(MemoryRecord(**d))
        return out

    async def delete_user(self, user_id: str, agent: str | None = None, tenant: str = "default") -> int:
        async with (await self.pool).acquire() as con:
            if agent is None:
                res = await con.execute("DELETE FROM user_memory WHERE user_id=$1 AND tenant=$2", user_id, tenant)
            else:
                res = await con.execute("DELETE FROM user_memory WHERE user_id=$1 AND agent=$2 AND tenant=$3", user_id, agent, tenant)
        return int(str(res).split()[-1])


class PostgresTenantToolStore(_Pooled, TenantToolStore):
    """Tool Registry API registrations in Postgres; the primary key settles duplicate names across replicas."""

    async def add(self, tenant: str, name: str, registration: dict[str, Any]) -> bool:
        async with (await self.pool).acquire() as con:
            res = await con.execute(
                "INSERT INTO tenant_tools (tenant, name, registration) VALUES ($1, $2, $3::jsonb) ON CONFLICT DO NOTHING",
                tenant,
                name,
                json.dumps(registration),
            )
        return str(res).endswith(" 1")

    async def list(self, tenant: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        async with (await self.pool).acquire() as con:
            if tenant is None:
                rows = await con.fetch("SELECT tenant, registration FROM tenant_tools ORDER BY tenant, name")
            else:
                rows = await con.fetch("SELECT tenant, registration FROM tenant_tools WHERE tenant=$1 ORDER BY name", tenant)
        return [(r["tenant"], json.loads(r["registration"])) for r in rows]


class PostgresDatasetStore(_Pooled, DatasetStore):
    """The dataset catalog in Postgres, shared by every replica."""

    async def get(self, tenant: str, name: str) -> Dataset | None:
        async with (await self.pool).acquire() as con:
            row = await con.fetchrow("SELECT dataset FROM datasets WHERE tenant=$1 AND name=$2", tenant, name)
        return Dataset.model_validate_json(row["dataset"]) if row else None

    async def list(self, tenant: str) -> list[Dataset]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch("SELECT dataset FROM datasets WHERE tenant=$1 ORDER BY name", tenant)
        return [Dataset.model_validate_json(r["dataset"]) for r in rows]

    async def put(self, dataset: Dataset) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO datasets (tenant, name, dataset, updated_at) VALUES ($1, $2, $3::jsonb, now()) "
                "ON CONFLICT (tenant, name) DO UPDATE SET dataset=EXCLUDED.dataset, updated_at=now()",
                dataset.tenant,
                dataset.name,
                dataset.model_dump_json(),
            )

    async def delete(self, tenant: str, name: str) -> bool:
        async with (await self.pool).acquire() as con:
            res = await con.execute("DELETE FROM datasets WHERE tenant=$1 AND name=$2", tenant, name)
        return str(res).endswith(" 1")


class PostgresAgentVersionStore(_Pooled, AgentVersionStore):
    """Saved policy versions in Postgres, shared by every replica. Version numbers come from the primary key: two
    concurrent saves cannot take the same number (the loser retries with the next one)."""

    COLS = "tenant, agent, version, hash, yaml, note, created_at"
    ACTIVE = (
        "SELECT v.tenant, v.agent, v.version, v.hash, v.yaml, v.note, v.created_at FROM agent_active a JOIN agent_versions v USING (tenant, agent, version)"
    )

    async def save(self, tenant: str, agent: str, policy_hash: str, yaml: str, note: str = "") -> tuple[PolicyVersion, bool]:
        async with (await self.pool).acquire() as con:
            for _ in range(5):
                row = await con.fetchrow(
                    f"INSERT INTO agent_versions (tenant, agent, version, hash, yaml, note) "
                    f"SELECT $1, $2, COALESCE(MAX(version), 0) + 1, $3, $4, $5 FROM agent_versions WHERE tenant=$1 AND agent=$2 "
                    f"ON CONFLICT DO NOTHING RETURNING {self.COLS}",
                    tenant,
                    agent,
                    policy_hash,
                    yaml,
                    note,
                )
                if row is not None:
                    return PolicyVersion(**dict(row)), True
                same = await con.fetchrow(f"SELECT {self.COLS} FROM agent_versions WHERE tenant=$1 AND agent=$2 AND hash=$3", tenant, agent, policy_hash)
                if same is not None:
                    return PolicyVersion(**dict(same)), False
        raise RuntimeError(f"could not number a new version of {tenant}/{agent}")  # sustained concurrent saves

    async def versions(self, tenant: str, agent: str) -> list[PolicyVersion]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch(f"SELECT {self.COLS} FROM agent_versions WHERE tenant=$1 AND agent=$2 ORDER BY version DESC", tenant, agent)
        return [PolicyVersion(**dict(r)) for r in rows]

    async def get(self, tenant: str, agent: str, version: int) -> PolicyVersion | None:
        async with (await self.pool).acquire() as con:
            row = await con.fetchrow(f"SELECT {self.COLS} FROM agent_versions WHERE tenant=$1 AND agent=$2 AND version=$3", tenant, agent, version)
        return PolicyVersion(**dict(row)) if row else None

    async def activate(self, tenant: str, agent: str, version: int) -> PolicyVersion | None:
        v = await self.get(tenant, agent, version)
        if v is None:
            return None
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO agent_active (tenant, agent, version) VALUES ($1, $2, $3) ON CONFLICT (tenant, agent) DO UPDATE SET version=EXCLUDED.version",
                tenant,
                agent,
                version,
            )
        return v

    async def active(self, tenant: str, agent: str) -> PolicyVersion | None:
        async with (await self.pool).acquire() as con:
            row = await con.fetchrow(f"{self.ACTIVE} WHERE a.tenant=$1 AND a.agent=$2", tenant, agent)
        return PolicyVersion(**dict(row)) if row else None

    async def agents(self, tenant: str) -> list[AgentSummary]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch(
                "SELECT v.agent, a.version AS active_version, MAX(v.version) AS latest_version, MAX(v.created_at) AS updated_at "
                "FROM agent_versions v LEFT JOIN agent_active a USING (tenant, agent) WHERE v.tenant=$1 "
                "GROUP BY v.agent, a.version ORDER BY v.agent",
                tenant,
            )
        return [AgentSummary(**dict(r)) for r in rows]

    async def all_active(self) -> list[PolicyVersion]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch(f"{self.ACTIVE} ORDER BY v.tenant, v.agent")
        return [PolicyVersion(**dict(r)) for r in rows]


class PostgresTemplateStore(_Pooled, TemplateStore):
    """A tenant's own bot types in Postgres."""

    COLS = "tenant, id, name, description, yaml, updated_at"

    async def save(self, tenant: str, template_id: str, name: str, description: str, yaml: str) -> PolicyTemplate:
        async with (await self.pool).acquire() as con:
            row = await con.fetchrow(
                f"INSERT INTO policy_templates (tenant, id, name, description, yaml) VALUES ($1, $2, $3, $4, $5) "
                f"ON CONFLICT (tenant, id) DO UPDATE SET name=EXCLUDED.name, description=EXCLUDED.description, yaml=EXCLUDED.yaml, "
                f"updated_at=now() RETURNING {self.COLS}",
                tenant,
                template_id,
                name,
                description,
                yaml,
            )
        return PolicyTemplate(**dict(row))

    async def list(self, tenant: str) -> list[PolicyTemplate]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch(f"SELECT {self.COLS} FROM policy_templates WHERE tenant=$1 ORDER BY id", tenant)
        return [PolicyTemplate(**dict(r)) for r in rows]

    async def get(self, tenant: str, template_id: str) -> PolicyTemplate | None:
        async with (await self.pool).acquire() as con:
            row = await con.fetchrow(f"SELECT {self.COLS} FROM policy_templates WHERE tenant=$1 AND id=$2", tenant, template_id)
        return PolicyTemplate(**dict(row)) if row else None

    async def delete(self, tenant: str, template_id: str) -> bool:
        async with (await self.pool).acquire() as con:
            res = await con.execute("DELETE FROM policy_templates WHERE tenant=$1 AND id=$2", tenant, template_id)
        return str(res).endswith(" 1")


class PostgresSessionStore(_Pooled, SessionStore):
    """Sessions in Postgres, so conversations survive restarts and are shared by replicas (no Redis needed). Writes
    are optimistic: a save succeeds only when the stored version is the one the turn started from. Unfinished
    sessions resume for `session_ttl_s` after the last turn; finished ones and dedupe records keep `dedupe_ttl_s`."""

    def __init__(self, pool: Any = None, url: str | None = None, dedupe_ttl_s: int = 86400, session_ttl_s: int = 7 * 86400):
        super().__init__(pool, url)
        self.ttl, self.session_ttl_s = dedupe_ttl_s, session_ttl_s

    async def load(self, session_id: str) -> SessionState | None:
        async with (await self.pool).acquire() as con:
            raw = await con.fetchval("SELECT state FROM sessions WHERE id=$1 AND expires_at > now()", session_id)
        return SessionState.model_validate_json(raw) if raw else None

    async def save(self, state: SessionState) -> SessionState:
        nxt = state.model_copy(deep=True, update={"version": state.version + 1})
        ttl = self.ttl if nxt.finished else self.session_ttl_s
        args = (nxt.id, nxt.tenant, nxt.agent, nxt.user_id, nxt.version, nxt.finished, ttl, nxt.model_dump_json())
        async with (await self.pool).acquire() as con:
            if state.version == 0:
                done = await con.fetchval(
                    "INSERT INTO sessions (id, tenant, agent, user_id, version, finished, expires_at, state) "
                    "VALUES ($1, $2, $3, $4, $5, $6, now() + make_interval(secs => $7), $8::jsonb) ON CONFLICT (id) DO NOTHING RETURNING 1",
                    *args,
                )
            else:
                done = await con.fetchval(
                    "UPDATE sessions SET version=$5, finished=$6, updated_at=now(), expires_at=now() + make_interval(secs => $7), state=$8::jsonb "
                    "WHERE id=$1 AND tenant=$2 AND agent=$3 AND user_id=$4 AND version=$5 - 1 AND expires_at > now() RETURNING 1",
                    *args,
                )
        if not done:
            raise VersionConflict(f"session {state.id}: expected v{state.version}, the stored version differs")
        return nxt

    async def find_open(self, user_id: str, agent: str, tenant: str = "default") -> SessionState | None:
        async with (await self.pool).acquire() as con:
            raw = await con.fetchval(
                "SELECT state FROM sessions WHERE tenant=$1 AND agent=$2 AND user_id=$3 AND NOT finished AND expires_at > now() "
                "ORDER BY updated_at DESC LIMIT 1",
                tenant,
                agent,
                user_id,
            )
        return SessionState.model_validate_json(raw) if raw else None

    async def seen(self, session_id: str, message_id: str) -> dict[str, Any] | None:
        async with (await self.pool).acquire() as con:
            raw = await con.fetchval("SELECT result FROM session_seen WHERE session_id=$1 AND message_id=$2 AND expires_at > now()", session_id, message_id)
        out: dict[str, Any] | None = json.loads(raw) if raw else None
        return out

    async def mark_seen(self, session_id: str, message_id: str, result: dict[str, Any]) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO session_seen (session_id, message_id, expires_at, result) VALUES ($1, $2, now() + make_interval(secs => $3), $4::jsonb) "
                "ON CONFLICT (session_id, message_id) DO NOTHING",
                session_id,
                message_id,
                self.ttl,
                json.dumps(result, default=str),
            )

    async def purge(self) -> int:
        """Delete expired sessions and dedupe records; returns how many sessions went."""
        async with (await self.pool).acquire() as con:
            n = await con.execute("DELETE FROM sessions WHERE expires_at <= now()")
            await con.execute("DELETE FROM session_seen WHERE expires_at <= now()")
        return int(str(n).split()[-1])


class PostgresRateLimiter(_Pooled, RateLimiter):
    """Rate limits every server shares. Each take is one transaction that locks its key (an advisory lock), drops the
    key's expired hits, counts the rest and records the new one, so concurrent servers never both take the last slot."""

    async def take(self, key: str, limit: int, window_s: float) -> bool:
        async with (await self.pool).acquire() as con, con.transaction():
            await con.execute("SELECT pg_advisory_xact_lock(hashtext($1))", key)
            await con.execute("DELETE FROM rate_hits WHERE key=$1 AND expires_at <= now()", key)
            if await con.fetchval("SELECT count(*) FROM rate_hits WHERE key=$1", key) >= limit:
                return False
            await con.execute("INSERT INTO rate_hits (key, at, expires_at) VALUES ($1, now(), now() + make_interval(secs => $2))", key, float(window_s))
        return True

    async def purge(self) -> int:
        """Drop expired hits of keys no one has used since."""
        async with (await self.pool).acquire() as con:
            res = await con.execute("DELETE FROM rate_hits WHERE expires_at <= now()")
        return int(str(res).split()[-1])


class PostgresTurnLocks(_Pooled, TurnLocks):
    """Per-conversation locks every server shares: a lease row per conversation. A server that dies mid-turn leaves its
    lease to run out (`lease_s`), so a conversation is never stuck; session saves are versioned besides."""

    def __init__(self, pool: Any = None, url: str | None = None, lease_s: float = 180):
        _Pooled.__init__(self, pool, url)
        TurnLocks.__init__(self)
        self.lease_s = lease_s

    async def acquire(self, key: str, holder: str) -> bool:
        async with (await self.pool).acquire() as con:
            got = await con.fetchval(
                "INSERT INTO turn_locks (key, holder, expires_at) VALUES ($1, $2, now() + make_interval(secs => $3)) ON CONFLICT (key) "
                "DO UPDATE SET holder=EXCLUDED.holder, expires_at=EXCLUDED.expires_at WHERE turn_locks.expires_at <= now() RETURNING 1",
                key,
                holder,
                float(self.lease_s),
            )
        return bool(got)

    async def release(self, key: str, holder: str) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute("DELETE FROM turn_locks WHERE key=$1 AND holder=$2", key, holder)

    async def purge(self) -> int:
        async with (await self.pool).acquire() as con:
            res = await con.execute("DELETE FROM turn_locks WHERE expires_at <= now()")
        return int(str(res).split()[-1])


class PostgresJobStore(_Pooled, JobStore):
    """Insight jobs, so a result outlives the request and the machine that ran it."""

    async def put(self, job: InsightJob) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO insight_jobs (id, tenant, created_at, job) VALUES ($1, $2, $3, $4::jsonb) ON CONFLICT (id) DO UPDATE SET job = EXCLUDED.job",
                job.id,
                job.tenant,
                job.created_at,
                job.model_dump_json(),
            )

    async def get(self, tenant: str, job_id: str) -> InsightJob | None:
        async with (await self.pool).acquire() as con:
            raw = await con.fetchval("SELECT job FROM insight_jobs WHERE id=$1 AND tenant=$2", job_id, tenant)
        return InsightJob.model_validate_json(raw) if raw else None

    async def purge(self, before: datetime) -> int:
        async with (await self.pool).acquire() as con:
            res = await con.execute("DELETE FROM insight_jobs WHERE created_at < $1", before)
        return int(str(res).split()[-1])


class PostgresBudgetStore(_Pooled, BudgetStore):
    """Workspace plans; usage is added with atomic increments, so concurrent turns never lose one."""

    async def get(self, tenant: str) -> Plan | None:
        async with (await self.pool).acquire() as con:
            r = await con.fetchrow("SELECT limit_turns, limit_usd, small_models, paid, turns, spent_usd, period FROM tenant_budgets WHERE tenant=$1", tenant)
        if r is None:
            return None
        month = store.usage_period()
        if r["period"] < month:  # nothing used yet this month
            return Plan(r["limit_turns"], r["limit_usd"], r["small_models"], r["paid"], 0, 0.0, month)
        return Plan(r["limit_turns"], r["limit_usd"], r["small_models"], r["paid"], r["turns"], float(r["spent_usd"]), month)

    async def set_plan(self, tenant: str, limit_turns: int | None, limit_usd: float | None, small_models: bool, paid: bool = False) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO tenant_budgets (tenant, limit_turns, limit_usd, small_models, paid) VALUES ($1, $2, $3, $4, $5) ON CONFLICT (tenant) "
                "DO UPDATE SET limit_turns=EXCLUDED.limit_turns, limit_usd=EXCLUDED.limit_usd, small_models=EXCLUDED.small_models, paid=EXCLUDED.paid",
                tenant,
                limit_turns,
                limit_usd,
                small_models,
                paid,
            )

    async def charge(self, tenant: str, usd: float, turns: int = 0) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute(  # one statement, so concurrent charges never lose one; only a newer month starts usage at zero,
                # so a charge computed just before midnight that lands after the new month began is added, not a reset
                "UPDATE tenant_budgets SET spent_usd = CASE WHEN period < $4 THEN 0 ELSE spent_usd END + $2, "
                "turns = CASE WHEN period < $4 THEN 0 ELSE turns END + $3, period = GREATEST(period, $4) WHERE tenant=$1",
                tenant,
                usd,
                turns,
                store.usage_period(),
            )


class PostgresApiKeyStore(_Pooled, ApiKeyStore):
    """Issued API keys: only the SHA-256 of each key is stored."""

    COLS = "id, tenant, name, scopes, hash, created_at, expires_at, last_used_at, revoked_at"

    @staticmethod
    def _row(r: Any) -> ApiKey:
        return ApiKey(**{**dict(r), "scopes": list(r["scopes"])})

    async def add(self, key: ApiKey) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO api_keys (id, tenant, name, scopes, hash, created_at, expires_at) VALUES ($1, $2, $3, $4, $5, $6, $7)",
                key.id,
                key.tenant,
                key.name,
                list(key.scopes),
                key.hash,
                key.created_at,
                key.expires_at,
            )

    async def get(self, key_id: str) -> ApiKey | None:
        async with (await self.pool).acquire() as con:
            row = await con.fetchrow(f"SELECT {self.COLS} FROM api_keys WHERE id=$1", key_id)
        return self._row(row) if row else None

    async def list(self, tenant: str) -> list[ApiKey]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch(f"SELECT {self.COLS} FROM api_keys WHERE tenant=$1 ORDER BY created_at", tenant)
        return [self._row(r) for r in rows]

    async def revoke(self, tenant: str, key_id: str) -> bool:
        async with (await self.pool).acquire() as con:
            res = await con.execute("UPDATE api_keys SET revoked_at=now() WHERE id=$1 AND tenant=$2 AND revoked_at IS NULL", key_id, tenant)
        return str(res).endswith(" 1")

    async def touch(self, key_id: str) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute("UPDATE api_keys SET last_used_at=now() WHERE id=$1", key_id)


def _audit_where(
    tenant: str, agent: str | None, session_id: str | None, kinds: builtins.list[AuditKind] | None, since: datetime | None, until: datetime | None
) -> tuple[str, builtins.list[Any]]:
    where, args = ["tenant=$1"], builtins.list[Any]([tenant])
    for col, val in (("agent", agent), ("session_id", session_id)):
        if val is not None:
            args.append(val)
            where.append(f"{col}=${len(args)}")
    if kinds:
        args.append(builtins.list(kinds))
        where.append(f"kind = ANY(${len(args)})")
    if since is not None:
        args.append(since)
        where.append(f"at >= ${len(args)}")
    if until is not None:
        args.append(until)
        where.append(f"at < ${len(args)}")
    return " AND ".join(where), args


_PG_TYPES = {"VARCHAR": "text", "BIGINT": "bigint", "DOUBLE": "float8", "BOOLEAN": "boolean"}
#: AUDIT_COLUMNS from the row: its own columns where it has them, else the record JSON
_AUDIT_SELECT = ", ".join(
    c if c in ("id", "tenant", "agent", "session_id", "kind", "at") else f"(event->>'{c}')::{_PG_TYPES[t]}" for c, t in AUDIT_COLUMNS.items()
)


class PostgresAuditStore(_Pooled, AuditStore):
    """The audit log in Postgres: one row per decision, the record as JSON plus the columns it is filtered by."""

    async def append(self, events: list[AuditEvent]) -> None:
        if not events:
            return
        async with (await self.pool).acquire() as con:
            await con.executemany(
                "INSERT INTO audit_events (id, tenant, agent, session_id, kind, at, event) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) ON CONFLICT DO NOTHING",
                [(e.id, e.tenant, e.agent, e.session_id, e.kind, e.at, e.model_dump_json()) for e in events],
            )

    async def query(
        self,
        tenant: str,
        *,
        agent: str | None = None,
        session_id: str | None = None,
        kinds: list[AuditKind] | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 200,
    ) -> list[AuditEvent]:
        where, args = _audit_where(tenant, agent, session_id, kinds, since, until)
        args.append(limit)
        sql = f"SELECT event FROM audit_events WHERE {where} ORDER BY at DESC, id DESC LIMIT ${len(args)}"
        async with (await self.pool).acquire() as con:
            rows = await con.fetch(sql, *args)
        return [AuditEvent.model_validate_json(r["event"]) for r in rows]

    async def purge(self, before: datetime) -> int:
        async with (await self.pool).acquire() as con:
            res = await con.execute("DELETE FROM audit_events WHERE at < $1", before)
        return int(str(res).split()[-1])

    async def forget(self, tenant: str, subject: str) -> int:
        async with (await self.pool).acquire() as con:
            res = await con.execute(
                "UPDATE audit_events SET event = jsonb_set(jsonb_set(event, '{text}', '\"\"'), '{reply}', '\"\"') || "
                "CASE WHEN kind = 'answer' THEN jsonb_build_object('reason', '') ELSE '{}'::jsonb END "
                "WHERE tenant=$1 AND event->>'subject' = $2 "
                "AND (event->>'text' <> '' OR event->>'reply' <> '' OR (kind = 'answer' AND event->>'reason' <> ''))",
                tenant,
                subject,
            )
        return int(str(res).split()[-1])

    async def between(self, after: datetime, until: datetime) -> list[AuditEvent]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch("SELECT event FROM audit_events WHERE at >= $1 AND at < $2 ORDER BY at, id", after, until)
        return [AuditEvent.model_validate_json(r["event"]) for r in rows]

    async def copy_to(
        self,
        db: DuckDBPyConnection,
        tenant: str,
        *,
        since: datetime,
        until: datetime | None = None,
        agent: str | None = None,
        kinds: builtins.list[AuditKind] | None = None,
    ) -> None:
        """COPY streams the window out as CSV (no row objects in Python); DuckDB reads the file."""
        where, args = _audit_where(tenant, agent, None, kinds, since, until)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.csv")
            async with (await self.pool).acquire() as con:
                await con.copy_from_query(f"SELECT {_AUDIT_SELECT} FROM audit_events WHERE {where}", *args, output=path, format="csv")
            columns = "{" + ", ".join(f"'{c}': '{t}'" for c, t in AUDIT_COLUMNS.items()) + "}"
            # an unquoted empty field is NULL, a quoted one ("") an empty string, as Postgres wrote them
            sql = f"INSERT INTO audit SELECT * FROM read_csv(?, header = false, allow_quoted_nulls = false, columns = {columns})"
            await asyncio.to_thread(db.execute, sql, [path])


class PostgresResponseStore(_Pooled, ResponseStore):
    async def add(self, r: SurveyResponse) -> None:
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO survey_responses (session_id, tenant, agent, user_id, completed_at, response) VALUES ($1,$2,$3,$4,$5,$6::jsonb) "
                "ON CONFLICT (session_id) DO UPDATE SET completed_at = EXCLUDED.completed_at, response = EXCLUDED.response",
                r.session_id,
                r.tenant,
                r.agent,
                r.user_id,
                r.completed_at,
                r.model_dump_json(),
            )

    async def list(self, tenant: str, agent: str, *, since: datetime | None = None, limit: int = 10_000) -> builtins.list[SurveyResponse]:
        sql = (
            "SELECT response FROM survey_responses WHERE tenant=$1 AND agent=$2 AND ($3::timestamptz IS NULL OR completed_at >= $3) "
            "ORDER BY completed_at DESC LIMIT $4"
        )
        async with (await self.pool).acquire() as con:
            rows = await con.fetch(sql, tenant, agent, since, limit)
        return [SurveyResponse.model_validate_json(r["response"]) for r in reversed(rows)]  # the newest `limit`, oldest first

    async def completed_since(self, tenant: str, since: datetime, limit: int = 500) -> builtins.list[SurveyResponse]:
        sql = (
            "SELECT response FROM survey_responses WHERE tenant=$1 AND completed_at > $2 AND coalesce((response->>'complete')::boolean, true) "
            "ORDER BY completed_at LIMIT $3"
        )
        async with (await self.pool).acquire() as con:
            rows = await con.fetch(sql, tenant, since, limit)
        return [SurveyResponse.model_validate_json(r["response"]) for r in rows]

    async def delete_user(self, tenant: str, user_id: str) -> int:
        async with (await self.pool).acquire() as con:
            res = await con.execute("DELETE FROM survey_responses WHERE tenant=$1 AND user_id=$2", tenant, user_id)
        return int(str(res).split()[-1])


class PostgresEmbedStore(_Pooled, EmbedStore):
    async def put(self, embed: Embed) -> Embed:
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO embeds (id, tenant, created_at, embed) VALUES ($1,$2,$3,$4::jsonb) ON CONFLICT (id) DO UPDATE SET embed = EXCLUDED.embed",
                embed.id,
                embed.tenant,
                embed.created_at,
                embed.model_dump_json(),
            )
        return embed

    async def get(self, embed_id: str) -> Embed | None:
        async with (await self.pool).acquire() as con:
            row = await con.fetchrow("SELECT embed FROM embeds WHERE id=$1", embed_id)
        return Embed.model_validate_json(row["embed"]) if row else None

    async def list(self, tenant: str) -> list[Embed]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch("SELECT embed FROM embeds WHERE tenant=$1 ORDER BY created_at DESC", tenant)
        return [Embed.model_validate_json(r["embed"]) for r in rows]


class PostgresHandoffStore(_Pooled, HandoffStore):
    """Handoffs in Postgres. Changes read and write the row under a lock, so a follow-up and a claim never lose each
    other's change."""

    async def create(self, handoff: Handoff) -> Handoff:
        async with (await self.pool).acquire() as con:
            await con.execute(
                "INSERT INTO handoffs (tenant, id, agent, status, created_at, handoff) VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
                handoff.tenant,
                handoff.id,
                handoff.agent,
                handoff.status,
                handoff.created_at,
                handoff.model_dump_json(),
            )
        return handoff

    async def get(self, tenant: str, handoff_id: str) -> Handoff | None:
        async with (await self.pool).acquire() as con:
            row = await con.fetchrow("SELECT handoff FROM handoffs WHERE tenant=$1 AND id=$2", tenant, handoff_id)
        return Handoff.model_validate_json(row["handoff"]) if row else None

    async def list(self, tenant: str, *, status: HandoffStatus | None = None, agent: str | None = None, limit: int = 100) -> list[Handoff]:
        async with (await self.pool).acquire() as con:
            rows = await con.fetch(
                "SELECT handoff FROM handoffs WHERE tenant=$1 AND ($2::text IS NULL OR status=$2) AND ($3::text IS NULL OR agent=$3) "
                "ORDER BY created_at DESC LIMIT $4",
                tenant,
                status,
                agent,
                limit,
            )
        return [Handoff.model_validate_json(r["handoff"]) for r in rows]

    async def _change(self, tenant: str, handoff_id: str, apply: Any) -> Handoff | None:
        async with (await self.pool).acquire() as con, con.transaction():
            row = await con.fetchrow("SELECT handoff FROM handoffs WHERE tenant=$1 AND id=$2 FOR UPDATE", tenant, handoff_id)
            if row is None:
                return None
            h = Handoff.model_validate_json(row["handoff"])
            apply(h)
            h.updated_at = now()
            await con.execute("UPDATE handoffs SET status=$3, handoff=$4::jsonb WHERE tenant=$1 AND id=$2", tenant, handoff_id, h.status, h.model_dump_json())
        return h

    async def add_messages(self, tenant: str, handoff_id: str, lines: builtins.list[HandoffLine]) -> Handoff | None:
        def apply(h: Handoff) -> None:
            h.transcript.extend(lines)
            h.status = "open" if h.status == "resolved" else h.status

        return await self._change(tenant, handoff_id, apply)

    async def update(
        self,
        tenant: str,
        handoff_id: str,
        *,
        status: HandoffStatus | None = None,
        assignee: str | None = None,
        note: str | None = None,
        external_id: str | None = None,
    ) -> Handoff | None:
        changes = {"status": status, "assignee": assignee, "note": note, "external_id": external_id}

        def apply(h: Handoff) -> None:
            for k, v in changes.items():
                if v is not None:
                    setattr(h, k, v)

        return await self._change(tenant, handoff_id, apply)

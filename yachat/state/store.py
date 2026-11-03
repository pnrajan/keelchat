"""Store ports and in-memory fakes."""

from __future__ import annotations

import asyncio
import builtins
import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from yachat.policy.compiler import CompiledPolicy
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
    new_id,
    now,
)


class VersionConflict(Exception):
    """Optimistic version check failed."""


class PolicyStore(ABC):
    @abstractmethod
    async def put(self, policy: CompiledPolicy) -> None: ...
    @abstractmethod
    async def get(self, policy_hash: str) -> CompiledPolicy | None: ...
    @abstractmethod
    async def latest(self, agent: str, tenant: str = "default") -> CompiledPolicy | None: ...
    @abstractmethod
    async def activate(self, policy_hash: str) -> None:
        """Make a registered policy the one new sessions of its agent start on."""

    @abstractmethod
    async def retire(self, policy_hash: str) -> None: ...
    @abstractmethod
    async def is_retired(self, policy_hash: str) -> bool: ...


class AgentVersionStore(ABC):
    """Saved policy versions per tenant and agent, and which one is active. The durable source of compiled policies:
    each replica rebuilds the active versions from their YAML at startup."""

    @abstractmethod
    async def save(self, tenant: str, agent: str, policy_hash: str, yaml: str, note: str = "") -> tuple[PolicyVersion, bool]:
        """Store the next version; the existing one (and False) when this agent already has a version with this hash."""

    @abstractmethod
    async def versions(self, tenant: str, agent: str) -> list[PolicyVersion]:
        """Newest first."""

    @abstractmethod
    async def get(self, tenant: str, agent: str, version: int) -> PolicyVersion | None: ...

    @abstractmethod
    async def activate(self, tenant: str, agent: str, version: int) -> PolicyVersion | None:
        """None when there is no such version."""

    @abstractmethod
    async def active(self, tenant: str, agent: str) -> PolicyVersion | None: ...

    @abstractmethod
    async def agents(self, tenant: str) -> list[AgentSummary]: ...

    @abstractmethod
    async def all_active(self) -> list[PolicyVersion]:
        """The active version of every agent of every tenant (startup reload)."""


class InMemoryAgentVersionStore(AgentVersionStore):
    def __init__(self) -> None:
        self._v: dict[tuple[str, str], list[PolicyVersion]] = {}
        self._active: dict[tuple[str, str], int] = {}

    async def save(self, tenant: str, agent: str, policy_hash: str, yaml: str, note: str = "") -> tuple[PolicyVersion, bool]:
        rows = self._v.setdefault((tenant, agent), [])
        if same := next((v for v in rows if v.hash == policy_hash), None):
            return same, False
        v = PolicyVersion(tenant=tenant, agent=agent, version=len(rows) + 1, hash=policy_hash, yaml=yaml, note=note)
        rows.append(v)
        return v, True

    async def versions(self, tenant: str, agent: str) -> list[PolicyVersion]:
        return list(reversed(self._v.get((tenant, agent), [])))

    async def get(self, tenant: str, agent: str, version: int) -> PolicyVersion | None:
        return next((v for v in self._v.get((tenant, agent), []) if v.version == version), None)

    async def activate(self, tenant: str, agent: str, version: int) -> PolicyVersion | None:
        v = await self.get(tenant, agent, version)
        if v is not None:
            self._active[(tenant, agent)] = version
        return v

    async def active(self, tenant: str, agent: str) -> PolicyVersion | None:
        n = self._active.get((tenant, agent))
        return await self.get(tenant, agent, n) if n is not None else None

    async def agents(self, tenant: str) -> list[AgentSummary]:
        return [
            AgentSummary(agent=a, active_version=self._active.get((t, a)), latest_version=rows[-1].version, updated_at=rows[-1].created_at)
            for (t, a), rows in sorted(self._v.items())
            if t == tenant
        ]

    async def all_active(self) -> list[PolicyVersion]:
        return [v for (t, a), n in sorted(self._active.items()) if (v := await self.get(t, a, n)) is not None]


class TemplateStore(ABC):
    """A tenant's own bot types (saved from one of its agents). Built-in types are not stored."""

    @abstractmethod
    async def save(self, tenant: str, template_id: str, name: str, description: str, yaml: str) -> PolicyTemplate:
        """Insert, or replace the tenant's template with this id."""

    @abstractmethod
    async def list(self, tenant: str) -> list[PolicyTemplate]: ...

    @abstractmethod
    async def get(self, tenant: str, template_id: str) -> PolicyTemplate | None: ...

    @abstractmethod
    async def delete(self, tenant: str, template_id: str) -> bool: ...


class InMemoryTemplateStore(TemplateStore):
    def __init__(self) -> None:
        self._t: dict[tuple[str, str], PolicyTemplate] = {}

    async def save(self, tenant: str, template_id: str, name: str, description: str, yaml: str) -> PolicyTemplate:
        t = PolicyTemplate(tenant=tenant, id=template_id, name=name, description=description, yaml=yaml)
        self._t[(tenant, template_id)] = t
        return t

    async def list(self, tenant: str) -> list[PolicyTemplate]:
        return [t for (tn, _), t in sorted(self._t.items()) if tn == tenant]

    async def get(self, tenant: str, template_id: str) -> PolicyTemplate | None:
        return self._t.get((tenant, template_id))

    async def delete(self, tenant: str, template_id: str) -> bool:
        return self._t.pop((tenant, template_id), None) is not None


class SessionStore(ABC):
    @abstractmethod
    async def load(self, session_id: str) -> SessionState | None: ...

    @abstractmethod
    async def save(self, state: SessionState) -> SessionState:
        """Write iff stored version == state.version; returns the state with version+1."""

    @abstractmethod
    async def find_open(self, user_id: str, agent: str, tenant: str = "default") -> SessionState | None:
        """The user's most recent unfinished session for this agent and tenant (expiry is the caller's rule)."""

    @abstractmethod
    async def seen(self, session_id: str, message_id: str) -> dict[str, Any] | None: ...
    @abstractmethod
    async def mark_seen(self, session_id: str, message_id: str, result: dict[str, Any]) -> None: ...


class MemoryStore(ABC):
    @abstractmethod
    async def upsert(self, record: MemoryRecord) -> None: ...
    @abstractmethod
    async def list(self, user_id: str, agent: str, tenant: str = "default") -> list[MemoryRecord]: ...
    @abstractmethod
    async def delete_user(self, user_id: str, agent: str | None = None, tenant: str = "default") -> int: ...


class TenantToolStore(ABC):
    """Tool Registry API registrations: the durable source of truth, loaded into each replica's registry."""

    @abstractmethod
    async def add(self, tenant: str, name: str, registration: dict[str, Any]) -> bool:
        """Insert once; False when the tenant already has a tool with that name (registrations are immutable)."""

    @abstractmethod
    async def list(self, tenant: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        """(tenant, registration) pairs, for one tenant or all."""


class InMemoryTenantToolStore(TenantToolStore):
    def __init__(self) -> None:
        self._r: dict[tuple[str, str], dict[str, Any]] = {}

    async def add(self, tenant: str, name: str, registration: dict[str, Any]) -> bool:
        if (tenant, name) in self._r:
            return False
        self._r[(tenant, name)] = dict(registration)
        return True

    async def list(self, tenant: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        return [(t, dict(r)) for (t, _), r in sorted(self._r.items()) if tenant is None or t == tenant]


class InMemoryPolicyStore(PolicyStore):
    def __init__(self) -> None:
        self.by_hash: dict[str, CompiledPolicy] = {}
        self._latest: dict[tuple[str, str], str] = {}
        self._retired: set[str] = set()

    async def put(self, policy: CompiledPolicy) -> None:
        self.by_hash[policy.hash] = policy
        self._latest[(policy.tenant, policy.agent)] = policy.hash

    async def get(self, policy_hash: str) -> CompiledPolicy | None:
        return self.by_hash.get(policy_hash)

    async def latest(self, agent: str, tenant: str = "default") -> CompiledPolicy | None:
        h = self._latest.get((tenant, agent))
        return self.by_hash.get(h) if h else None

    async def activate(self, policy_hash: str) -> None:
        p = self.by_hash[policy_hash]
        self._latest[(p.tenant, p.agent)] = policy_hash

    async def retire(self, policy_hash: str) -> None:
        self._retired.add(policy_hash)

    async def is_retired(self, policy_hash: str) -> bool:
        return policy_hash in self._retired


class InMemorySessionStore(SessionStore):
    def __init__(self) -> None:
        self._s: dict[str, SessionState] = {}
        self._seen: dict[tuple[str, str], dict[str, Any]] = {}

    async def load(self, session_id: str) -> SessionState | None:
        st = self._s.get(session_id)
        return st.model_copy(deep=True) if st else None

    async def save(self, state: SessionState) -> SessionState:
        cur = self._s.get(state.id)
        stored = cur.version if cur else None
        if (stored is None and state.version != 0) or (stored is not None and stored != state.version):
            raise VersionConflict(f"session {state.id}: expected v{state.version}, stored v{stored}")
        nxt = state.model_copy(deep=True, update={"version": state.version + 1})
        self._s[state.id] = nxt
        return nxt.model_copy(deep=True)

    async def find_open(self, user_id: str, agent: str, tenant: str = "default") -> SessionState | None:
        open_ = [s for s in self._s.values() if s.user_id == user_id and s.agent == agent and s.tenant == tenant and not s.finished]
        return max(open_, key=lambda s: s.last_turn_at).model_copy(deep=True) if open_ else None

    async def seen(self, session_id: str, message_id: str) -> dict[str, Any] | None:
        return self._seen.get((session_id, message_id))

    async def mark_seen(self, session_id: str, message_id: str, result: dict[str, Any]) -> None:
        self._seen[(session_id, message_id)] = result


class InMemoryMemoryStore(MemoryStore):
    def __init__(self) -> None:
        self._r: dict[tuple[str, str, str, str], MemoryRecord] = {}

    async def upsert(self, record: MemoryRecord) -> None:
        key = (record.tenant, record.user_id, record.agent, record.item)
        prev = self._r.get(key)
        if prev:
            record = record.model_copy(update={"created_at": prev.created_at})
        self._r[key] = record

    async def list(self, user_id: str, agent: str, tenant: str = "default") -> list[MemoryRecord]:
        return [r for (t, u, a, _), r in self._r.items() if t == tenant and u == user_id and a == agent]

    async def delete_user(self, user_id: str, agent: str | None = None, tenant: str = "default") -> int:
        keys = [k for k in self._r if k[0] == tenant and k[1] == user_id and (agent is None or k[2] == agent)]
        for k in keys:
            del self._r[k]
        return len(keys)


class AuditStore(ABC):
    """The audit log: append-only decision records per tenant, newest first on read."""

    @abstractmethod
    async def append(self, events: list[AuditEvent]) -> None: ...

    @abstractmethod
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
    ) -> list[AuditEvent]: ...

    @abstractmethod
    async def purge(self, before: datetime) -> int:
        """Delete every record older than `before` (retention); returns how many."""

    @abstractmethod
    async def forget(self, tenant: str, subject: str) -> int:
        """Clear what a user wrote from their records (message snippets, rejected-answer snippets); the decisions and
        counts stay. Returns how many records changed."""

    @abstractmethod
    async def between(self, after: datetime, until: datetime) -> list[AuditEvent]:
        """Every tenant's records with after <= at < until, oldest first (the archive export reads these)."""


class InMemoryAuditStore(AuditStore):
    def __init__(self) -> None:
        self._e: list[AuditEvent] = []

    async def append(self, events: list[AuditEvent]) -> None:
        self._e.extend(e.model_copy() for e in events)

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
        out = [
            e
            for e in reversed(self._e)
            if e.tenant == tenant
            and (agent is None or e.agent == agent)
            and (session_id is None or e.session_id == session_id)
            and (not kinds or e.kind in kinds)
            and (since is None or e.at >= since)
            and (until is None or e.at < until)
        ]
        return [e.model_copy() for e in out[:limit]]

    async def forget(self, tenant: str, subject: str) -> int:
        n = 0
        for e in self._e:
            if e.tenant == tenant and e.subject == subject and (e.text or e.reply or (e.kind == "answer" and e.reason)):
                e.text = e.reply = ""
                if e.kind == "answer":
                    e.reason = ""
                n += 1
        return n

    async def purge(self, before: datetime) -> int:
        keep = [e for e in self._e if e.at >= before]
        n, self._e = len(self._e) - len(keep), keep
        return n

    async def between(self, after: datetime, until: datetime) -> list[AuditEvent]:
        return sorted((e.model_copy() for e in self._e if after <= e.at < until), key=lambda e: (e.at, e.id))


class ArchiveConflict(Exception):
    """Another writer moved the archive's cursor first; the export runs again later from the new cursor."""


class AuditArchive(ABC):
    """Long-term audit records (Iceberg on S3). Records stamped before `cursor()` are archived."""

    @abstractmethod
    async def cursor(self) -> datetime | None: ...

    @abstractmethod
    async def write(self, events: list[AuditEvent], *, start: datetime | None, end: datetime) -> None:
        """Add the records stamped in [start, end) and move the cursor from `start` to `end`, in one commit.
        ArchiveConflict when the cursor is no longer `start`."""

    @abstractmethod
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
        """Newest first, as AuditStore.query."""

    @abstractmethod
    async def purge(self, before: datetime) -> None:
        """Delete records older than `before`, from storage as well as from the table."""


class InMemoryAuditArchive(AuditArchive):
    def __init__(self) -> None:
        self._events = InMemoryAuditStore()
        self._cursor: datetime | None = None

    async def cursor(self) -> datetime | None:
        return self._cursor

    async def write(self, events: list[AuditEvent], *, start: datetime | None, end: datetime) -> None:
        if self._cursor != start:
            raise ArchiveConflict(f"archive cursor is {self._cursor}, not {start}")
        await self._events.append(events)
        self._cursor = end

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
        return await self._events.query(tenant, agent=agent, session_id=session_id, kinds=kinds, since=since, until=until, limit=limit)

    async def purge(self, before: datetime) -> None:
        await self._events.purge(before)


class ArchivedAuditStore(AuditStore):
    """Recent records in a hot store (Postgres), older ones in an archive (Iceberg). Writes go to the hot store; an
    export copies settled records to the archive; reads before the archive's cursor come from the archive, the rest
    from the hot store, which keeps `hot_days` of archived records."""

    #: records younger than this are not exported yet: a turn's records are written when it ends, stamped when it started
    LAG = timedelta(minutes=10)
    #: records per archive commit at most (plus one window's)
    BATCH = 50_000
    WINDOW = timedelta(hours=1)

    def __init__(self, hot: AuditStore, archive: AuditArchive, *, hot_days: int = 7) -> None:
        self.hot, self.archive, self.hot_days = hot, archive, hot_days

    async def append(self, events: list[AuditEvent]) -> None:
        await self.hot.append(events)

    async def forget(self, tenant: str, subject: str) -> int:
        """Clears the hot store. Archived records keep their snippets until the archive's own retention removes them."""
        return await self.hot.forget(tenant, subject)

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
        cur = await self.archive.cursor()
        if cur is None:
            return await self.hot.query(tenant, agent=agent, session_id=session_id, kinds=kinds, since=since, until=until, limit=limit)
        recent = await self.hot.query(
            tenant, agent=agent, session_id=session_id, kinds=kinds, since=max(since, cur) if since else cur, until=until, limit=limit
        )
        if len(recent) >= limit or (since is not None and since >= cur):
            return recent
        older = await self.archive.query(
            tenant, agent=agent, session_id=session_id, kinds=kinds, since=since, until=min(until, cur) if until else cur, limit=limit - len(recent)
        )
        return recent + older

    async def purge(self, before: datetime) -> int:
        """Retention in both; the hot store also drops archived records older than `hot_days`, never unarchived ones."""
        await self.archive.purge(before)
        cur = await self.archive.cursor()
        keep_from = before if cur is None else max(before, min(now() - timedelta(days=self.hot_days), cur))
        return await self.hot.purge(keep_from)

    async def between(self, after: datetime, until: datetime) -> list[AuditEvent]:
        return await self.hot.between(after, until)

    async def export(self, first: datetime) -> int:
        """Copy the hot records the archive does not have yet, up to LAG ago; `first`: where a new archive starts.
        Returns how many were archived. ArchiveConflict when another exporter got there first."""
        start = await self.archive.cursor()
        begin, stop, n = start or first, now() - self.LAG, 0
        while begin < stop:
            end, batch = begin, list[AuditEvent]()
            while end < stop and len(batch) < self.BATCH:
                nxt = min(end + self.WINDOW, stop)
                batch += await self.hot.between(end, nxt)
                end = nxt
            await self.archive.write(batch, start=start, end=end)
            n += len(batch)
            start = begin = end
        return n


class ResponseStore(ABC):
    """Finished surveys, per workspace and agent."""

    @abstractmethod
    async def add(self, r: SurveyResponse) -> None:
        """Idempotent per session: a session's later response replaces its earlier one."""

    @abstractmethod
    async def list(self, tenant: str, agent: str, *, since: datetime | None = None, limit: int = 10_000) -> builtins.list[SurveyResponse]:
        """Oldest first; past `limit`, the newest `limit` responses."""

    @abstractmethod
    async def delete_user(self, tenant: str, user_id: str) -> int: ...

    @abstractmethod
    async def completed_since(self, tenant: str, since: datetime, limit: int = 500) -> builtins.list[SurveyResponse]:
        """Finished responses of every survey of the workspace updated after `since`, oldest first (for syncing out)."""


class InMemoryResponseStore(ResponseStore):
    def __init__(self) -> None:
        self._r: dict[str, SurveyResponse] = {}

    async def add(self, r: SurveyResponse) -> None:
        self._r[r.session_id] = r.model_copy()

    async def list(self, tenant: str, agent: str, *, since: datetime | None = None, limit: int = 10_000) -> builtins.list[SurveyResponse]:
        out = [r for r in self._r.values() if r.tenant == tenant and r.agent == agent and (since is None or r.completed_at >= since)]
        return [r.model_copy() for r in sorted(out, key=lambda r: r.completed_at)[-limit:]] if limit > 0 else []

    async def completed_since(self, tenant: str, since: datetime, limit: int = 500) -> builtins.list[SurveyResponse]:
        out = sorted((r for r in self._r.values() if r.tenant == tenant and r.complete and r.completed_at > since), key=lambda r: r.completed_at)
        return [r.model_copy() for r in out[:limit]]

    async def delete_user(self, tenant: str, user_id: str) -> int:
        gone = [k for k, r in self._r.items() if r.tenant == tenant and r.user_id == user_id]
        for k in gone:
            del self._r[k]
        return len(gone)


class EmbedStore(ABC):
    """Agents published to the web (widget, app). Looked up by the public id alone: the record names its tenant."""

    @abstractmethod
    async def put(self, embed: Embed) -> Embed: ...

    @abstractmethod
    async def get(self, embed_id: str) -> Embed | None: ...

    @abstractmethod
    async def list(self, tenant: str) -> builtins.list[Embed]:
        """Newest first."""


class InMemoryEmbedStore(EmbedStore):
    def __init__(self) -> None:
        self._e: dict[str, Embed] = {}

    async def put(self, embed: Embed) -> Embed:
        self._e[embed.id] = embed.model_copy()
        return embed

    async def get(self, embed_id: str) -> Embed | None:
        e = self._e.get(embed_id)
        return e.model_copy() if e else None

    async def list(self, tenant: str) -> builtins.list[Embed]:
        return sorted((e.model_copy() for e in self._e.values() if e.tenant == tenant), key=lambda e: e.created_at, reverse=True)


class HandoffStore(ABC):
    """Conversations passed to a person: the inbox's source of truth."""

    @abstractmethod
    async def create(self, handoff: Handoff) -> Handoff: ...

    @abstractmethod
    async def get(self, tenant: str, handoff_id: str) -> Handoff | None: ...

    @abstractmethod
    async def list(self, tenant: str, *, status: HandoffStatus | None = None, agent: str | None = None, limit: int = 100) -> list[Handoff]:
        """Newest first."""

    @abstractmethod
    async def add_messages(self, tenant: str, handoff_id: str, lines: builtins.list[HandoffLine]) -> Handoff | None:
        """Append to the transcript; a resolved handoff the user writes to again is open again."""

    @abstractmethod
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
        """Change the given properties only; None when there is no such handoff."""


class InMemoryHandoffStore(HandoffStore):
    def __init__(self) -> None:
        self._h: dict[tuple[str, str], Handoff] = {}

    async def create(self, handoff: Handoff) -> Handoff:
        self._h[(handoff.tenant, handoff.id)] = handoff.model_copy(deep=True)
        return handoff.model_copy(deep=True)

    async def get(self, tenant: str, handoff_id: str) -> Handoff | None:
        h = self._h.get((tenant, handoff_id))
        return h.model_copy(deep=True) if h else None

    async def list(self, tenant: str, *, status: HandoffStatus | None = None, agent: str | None = None, limit: int = 100) -> list[Handoff]:
        rows = [h for (t, _), h in self._h.items() if t == tenant and (status is None or h.status == status) and (agent is None or h.agent == agent)]
        return [h.model_copy(deep=True) for h in sorted(rows, key=lambda h: h.created_at, reverse=True)[:limit]]

    async def add_messages(self, tenant: str, handoff_id: str, lines: builtins.list[HandoffLine]) -> Handoff | None:
        h = self._h.get((tenant, handoff_id))
        if h is None:
            return None
        h.transcript.extend(lines)
        h.status = "open" if h.status == "resolved" else h.status
        h.updated_at = now()
        return h.model_copy(deep=True)

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
        h = self._h.get((tenant, handoff_id))
        if h is None:
            return None
        changes = {"status": status, "assignee": assignee, "note": note, "external_id": external_id}
        for k, v in changes.items():
            if v is not None:
                setattr(h, k, v)
        h.updated_at = now()
        return h.model_copy(deep=True)


def usage_period() -> str:
    """Usage is counted per calendar month (UTC): limits like the free plan's 100 replies start again each month."""
    return datetime.now(UTC).strftime("%Y-%m")


@dataclass
class Plan:
    """A workspace's limits, per calendar month. None: no limit of that kind. Usage is counted only for tenants that
    have a plan."""

    limit_turns: int | None = None
    limit_usd: float | None = None
    small_models: bool = False
    #: paid features (slide decks); a tenant with no plan (self-hosted, admin) has every feature
    paid: bool = False
    turns: int = 0
    spent_usd: float = 0.0
    #: the month `turns` and `spent_usd` belong to
    period: str = ""

    def exhausted(self) -> bool:
        return (self.limit_turns is not None and self.turns >= self.limit_turns) or (self.limit_usd is not None and self.spent_usd >= self.limit_usd)


class RateLimiter(ABC):
    """Sliding-window counters: at most `limit` takes per `key` in any `window_s` seconds."""

    @abstractmethod
    async def take(self, key: str, limit: int, window_s: float) -> bool: ...


class InMemoryRateLimiter(RateLimiter):
    """Counters in this process: each server keeps its own."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def take(self, key: str, limit: int, window_s: float, now: float | None = None) -> bool:
        now = now or time.monotonic()
        q = self._hits[key]
        while q and q[0] <= now - window_s:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


class TurnLocks(ABC):
    """One holder per key (a conversation) at a time. Inside a process an asyncio lock queues callers; `acquire` and
    `release` extend that across servers, so two servers never run turns of one conversation at once."""

    #: how often a waiting server asks again
    POLL_S = 0.1

    def __init__(self) -> None:
        self._local: dict[str, asyncio.Lock] = {}

    @abstractmethod
    async def acquire(self, key: str, holder: str) -> bool:
        """Take the key for `holder` if no one else holds it; one attempt."""

    @abstractmethod
    async def release(self, key: str, holder: str) -> None: ...

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        async with self._local.setdefault(key, asyncio.Lock()):
            holder = new_id("lock")
            while not await self.acquire(key, holder):
                await asyncio.sleep(self.POLL_S)
            try:
                yield
            finally:
                await self.release(key, holder)


class InMemoryTurnLocks(TurnLocks):
    """One process: the asyncio lock is the whole lock."""

    async def acquire(self, key: str, holder: str) -> bool:
        return True

    async def release(self, key: str, holder: str) -> None:
        return None


class BudgetStore(ABC):
    """Workspace plans: limits on conversation turns and model spend, and whether only small models may run. A tenant
    with no plan is unlimited and is not tracked."""

    @abstractmethod
    async def get(self, tenant: str) -> Plan | None: ...

    @abstractmethod
    async def set_plan(self, tenant: str, limit_turns: int | None, limit_usd: float | None, small_models: bool, paid: bool = False) -> None:
        """Set or change the tenant's limits; what it has used is kept."""

    @abstractmethod
    async def charge(self, tenant: str, usd: float, turns: int = 0) -> None:
        """Add to what a tenant with a plan has used this month (a no-op for tenants without one)."""


class InMemoryBudgetStore(BudgetStore):
    def __init__(self) -> None:
        self._p: dict[str, Plan] = {}

    def _current(self, p: Plan) -> Plan:
        if p.period < (month := usage_period()):  # a new month: usage starts again (never back to an older one)
            p.turns, p.spent_usd, p.period = 0, 0.0, month
        return p

    async def get(self, tenant: str) -> Plan | None:
        p = self._p.get(tenant)
        return replace(self._current(p)) if p else None

    async def set_plan(self, tenant: str, limit_turns: int | None, limit_usd: float | None, small_models: bool, paid: bool = False) -> None:
        p = self._p.setdefault(tenant, Plan())
        p.limit_turns, p.limit_usd, p.small_models, p.paid = limit_turns, limit_usd, small_models, paid

    async def charge(self, tenant: str, usd: float, turns: int = 0) -> None:
        if p := self._p.get(tenant):
            self._current(p)
            p.spent_usd += usd
            p.turns += turns


class InsightJob(BaseModel):
    """An insights request run in the background. `result` holds the Insights and `words` the checked title and
    summary, so the report and the deck are rendered from it without calling a model again."""

    id: str = Field(default_factory=lambda: new_id("job"))
    tenant: str
    status: Literal["running", "done", "failed"] = "running"
    request: dict[str, Any]
    created_at: datetime = Field(default_factory=now)
    finished_at: datetime | None = None
    error: str = ""
    result: dict[str, Any] | None = None
    words: dict[str, Any] | None = None


class JobStore(ABC):
    @abstractmethod
    async def put(self, job: InsightJob) -> None: ...

    @abstractmethod
    async def get(self, tenant: str, job_id: str) -> InsightJob | None: ...


class InMemoryJobStore(JobStore):
    def __init__(self) -> None:
        self._j: dict[str, InsightJob] = {}

    async def put(self, job: InsightJob) -> None:
        self._j[job.id] = job.model_copy(deep=True)

    async def get(self, tenant: str, job_id: str) -> InsightJob | None:
        j = self._j.get(job_id)
        return j.model_copy(deep=True) if j and j.tenant == tenant else None

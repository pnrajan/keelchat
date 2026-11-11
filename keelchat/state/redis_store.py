"""Redis SessionStore: optimistic versioning via WATCH/MULTI, dedupe keys with TTL."""

from __future__ import annotations

import json
from typing import Any

from keelchat.state.models import SessionState
from keelchat.state.store import SessionStore, VersionConflict

try:  # optional extra
    import redis.asyncio as aioredis
    from redis.exceptions import WatchError
except ImportError:  # pragma: no cover
    aioredis = None  # type: ignore[assignment]
    WatchError = Exception  # type: ignore[assignment,misc]


class RedisSessionStore(SessionStore):
    def __init__(self, client: Any, prefix: str = "keelchat:", dedupe_ttl_s: int = 86400, session_ttl_s: int = 7 * 86400):
        if aioredis is None:  # pragma: no cover
            raise RuntimeError("install keelchat[redis] to use RedisSessionStore")
        self.r, self.prefix, self.ttl, self.session_ttl_s = client, prefix, dedupe_ttl_s, session_ttl_s

    @classmethod
    def from_url(cls, url: str) -> RedisSessionStore:
        return cls(aioredis.from_url(url, decode_responses=True))

    def _k(self, session_id: str) -> str:
        return f"{self.prefix}session:{session_id}"

    async def load(self, session_id: str) -> SessionState | None:
        raw = await self.r.get(self._k(session_id))
        return SessionState.model_validate_json(raw) if raw else None

    async def save(self, state: SessionState) -> SessionState:
        key = self._k(state.id)
        async with self.r.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(key)
                raw = await pipe.get(key)
                stored = SessionState.model_validate_json(raw).version if raw else None
                if (stored is None and state.version != 0) or (stored is not None and stored != state.version):
                    raise VersionConflict(f"session {state.id}: expected v{state.version}, stored v{stored}")
                nxt = state.model_copy(deep=True, update={"version": state.version + 1})
                pipe.multi()
                if nxt.finished:  # completed or exited sessions close: no resume; short retention unless a person may hand it back
                    await pipe.set(key, nxt.model_dump_json(), ex=self.ttl if nxt.retained_short else self.session_ttl_s)
                    await pipe.delete(self._open_key(nxt.user_id, nxt.agent, nxt.tenant))
                else:  # unfinished sessions resume within the TTL from the last turn
                    await pipe.set(key, nxt.model_dump_json(), ex=self.session_ttl_s)
                    await pipe.set(self._open_key(nxt.user_id, nxt.agent, nxt.tenant), nxt.id, ex=self.session_ttl_s)
                await pipe.execute()
                return nxt
            except WatchError:
                raise VersionConflict(f"session {state.id}: concurrent write") from None

    def _open_key(self, user_id: str, agent: str, tenant: str) -> str:
        return f"{self.prefix}open:{tenant}:{agent}:{user_id}"

    async def find_open(self, user_id: str, agent: str, tenant: str = "default") -> SessionState | None:
        sid = await self.r.get(self._open_key(user_id, agent, tenant))
        return await self.load(sid) if sid else None

    async def seen(self, session_id: str, message_id: str) -> dict[str, Any] | None:
        raw = await self.r.get(f"{self.prefix}seen:{session_id}:{message_id}")
        return json.loads(raw) if raw else None

    async def mark_seen(self, session_id: str, message_id: str, result: dict[str, Any]) -> None:
        await self.r.set(f"{self.prefix}seen:{session_id}:{message_id}", json.dumps(result, default=str), ex=self.ttl)

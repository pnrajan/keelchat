"""User memory in mem0: the hosted platform (MEM0_API_KEY) or self-hosted mem0 on Qdrant (MEM0_QDRANT_URL or
MEM0_QDRANT_PATH), with SiliconFlow embeddings.

Layers: `Mem0MemoryStore` implements the runtime's `MemoryStore` port and knows nothing of mem0's library; it talks to a
`Mem0Backend`, a four-call interface (add, rows, update, delete). The library is used in exactly two places, the
backends `HostedMem0` and `LocalMem0`, and `InMemoryMem0` stands in for it in tests.

mem0 is the store, not the judge. Memories are written with inference off, so mem0 never extracts its own "facts"
from what people say: only the items a policy lists, after consent and never from flagged turns, are written (the
MemoryWriter decides, law 8). Every memory is scoped to its workspace, user and agent: the mem0 user id is
`<tenant>/<user>` and the agent id is the agent, so one workspace can never read or delete another's memories. mem0's
own usage telemetry is off unless MEM0_TELEMETRY is set."""

from __future__ import annotations

import builtins
import itertools
import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from keelchat.logging import get_logger
from keelchat.state.models import MemoryRecord
from keelchat.state.store import MemoryStore

log = get_logger("mem0")


@dataclass
class Mem0Row:
    """One stored memory, as a backend returns it."""

    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Mem0Backend(ABC):
    """What the memory store needs from mem0, and nothing more."""

    @abstractmethod
    async def add(self, text: str, *, user_id: str, agent_id: str, metadata: dict[str, Any]) -> None:
        """Store `text` as written (never inferred from)."""

    @abstractmethod
    async def rows(self, *, user_id: str, agent_id: str | None) -> builtins.list[Mem0Row]:
        """Every memory of a user (and agent, when given)."""

    @abstractmethod
    async def update(self, row_id: str, *, text: str, metadata: dict[str, Any]) -> None: ...

    @abstractmethod
    async def delete(self, row_id: str) -> None: ...


def _rows(result: Any) -> builtins.list[Mem0Row]:
    items = result.get("results", []) if isinstance(result, dict) else result or []
    return [Mem0Row(id=str(m["id"]), text=str(m.get("memory") or ""), metadata=dict(m.get("metadata") or {})) for m in items]


class HostedMem0(Mem0Backend):
    """mem0's hosted platform (AsyncMemoryClient)."""

    def __init__(self, api_key: str, org_id: str | None = None, project_id: str | None = None):
        from mem0 import AsyncMemoryClient

        self.client = AsyncMemoryClient(api_key=api_key, org_id=org_id, project_id=project_id)

    async def add(self, text: str, *, user_id: str, agent_id: str, metadata: dict[str, Any]) -> None:
        await self.client.add([{"role": "user", "content": text}], user_id=user_id, agent_id=agent_id, metadata=metadata, infer=False)

    async def rows(self, *, user_id: str, agent_id: str | None) -> builtins.list[Mem0Row]:
        filters: dict[str, Any] = {"AND": [{"user_id": user_id}] + ([{"agent_id": agent_id}] if agent_id else [])}
        return _rows(await self.client.get_all(filters=filters, page_size=200))

    async def update(self, row_id: str, *, text: str, metadata: dict[str, Any]) -> None:
        await self.client.update(row_id, text=text, metadata=metadata)

    async def delete(self, row_id: str) -> None:
        await self.client.delete(row_id)


class LocalMem0(Mem0Backend):
    """Self-hosted mem0 (AsyncMemory) on Qdrant, embedding with an OpenAI-compatible host (SiliconFlow)."""

    def __init__(self, config: dict[str, Any]):
        from mem0 import AsyncMemory

        self.client = AsyncMemory.from_config(config)

    async def add(self, text: str, *, user_id: str, agent_id: str, metadata: dict[str, Any]) -> None:
        await self.client.add([{"role": "user", "content": text}], user_id=user_id, agent_id=agent_id, metadata=metadata, infer=False)

    async def rows(self, *, user_id: str, agent_id: str | None) -> builtins.list[Mem0Row]:
        return _rows(await self.client.get_all(filters={"user_id": user_id, **({"agent_id": agent_id} if agent_id else {})}, top_k=1000))

    async def update(self, row_id: str, *, text: str, metadata: dict[str, Any]) -> None:
        await self.client.update(row_id, text=text, metadata=metadata)

    async def delete(self, row_id: str) -> None:
        await self.client.delete(row_id)


class InMemoryMem0(Mem0Backend):
    """A fake backend: the store's logic tested without mem0 or a network."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, str, Mem0Row]] = {}
        self._ids = itertools.count(1)

    async def add(self, text: str, *, user_id: str, agent_id: str, metadata: dict[str, Any]) -> None:
        rid = f"m{next(self._ids)}"
        self._rows[rid] = (user_id, agent_id, Mem0Row(rid, text, dict(metadata)))

    async def rows(self, *, user_id: str, agent_id: str | None) -> builtins.list[Mem0Row]:
        return [r for u, a, r in self._rows.values() if u == user_id and (agent_id is None or a == agent_id)]

    async def update(self, row_id: str, *, text: str, metadata: dict[str, Any]) -> None:
        u, a, _ = self._rows[row_id]
        self._rows[row_id] = (u, a, Mem0Row(row_id, text, dict(metadata)))

    async def delete(self, row_id: str) -> None:
        self._rows.pop(row_id, None)


def scoped_user(tenant: str, user_id: str) -> str:
    """The mem0 user id: the workspace and the user, so ids that repeat across workspaces stay apart."""
    if "/" in tenant:
        raise ValueError(f"tenant {tenant!r} cannot contain '/'")
    return f"{tenant}/{user_id}"


def _text(r: MemoryRecord) -> str:
    return f"{r.item}: {json.dumps(r.value, ensure_ascii=False)}"


def _meta(r: MemoryRecord) -> dict[str, Any]:
    return {"keelchat": r.model_dump(mode="json"), "tenant": r.tenant, "item": r.item}


class Mem0MemoryStore(MemoryStore):
    """The runtime's MemoryStore over any Mem0Backend: one memory per workspace, user, agent and item."""

    def __init__(self, backend: Mem0Backend):
        self.backend = backend

    async def _all(self, tenant: str, user_id: str, agent: str | None) -> builtins.list[Mem0Row]:
        rows = await self.backend.rows(user_id=scoped_user(tenant, user_id), agent_id=agent)
        # a row is ours only if its metadata names this workspace; anything else is ignored, never returned
        return [r for r in rows if r.metadata.get("tenant") == tenant and r.metadata.get("keelchat")]

    async def upsert(self, record: MemoryRecord) -> None:
        existing = [r for r in await self._all(record.tenant, record.user_id, record.agent) if r.metadata.get("item") == record.item]
        if existing:  # one memory per user, agent and item: replace it, keep the first created_at
            first = MemoryRecord.model_validate(existing[0].metadata["keelchat"])
            record = record.model_copy(update={"created_at": first.created_at})
            await self.backend.update(existing[0].id, text=_text(record), metadata=_meta(record))
            for extra in existing[1:]:
                await self.backend.delete(extra.id)
            return
        await self.backend.add(_text(record), user_id=scoped_user(record.tenant, record.user_id), agent_id=record.agent, metadata=_meta(record))

    async def list(self, user_id: str, agent: str, tenant: str = "default") -> builtins.list[MemoryRecord]:
        return [MemoryRecord.model_validate(r.metadata["keelchat"]) for r in await self._all(tenant, user_id, agent)]

    async def delete_user(self, user_id: str, agent: str | None = None, tenant: str = "default") -> int:
        rows = await self._all(tenant, user_id, agent)
        for r in rows:
            await self.backend.delete(r.id)
        return len(rows)


def mem0_from_env() -> Mem0MemoryStore | None:
    """MEM0_API_KEY → the hosted platform (MEM0_ORG_ID, MEM0_PROJECT_ID optional). MEM0_QDRANT_URL or
    MEM0_QDRANT_PATH → self-hosted mem0, embeddings from SiliconFlow (KNOWLEDGE_EMBED_MODEL). None: not configured."""
    os.environ.setdefault("MEM0_TELEMETRY", "false")  # no usage data leaves unless an operator turns it on
    if key := os.getenv("MEM0_API_KEY"):
        log.info("memory in mem0 (hosted)")
        return Mem0MemoryStore(HostedMem0(key, os.getenv("MEM0_ORG_ID") or None, os.getenv("MEM0_PROJECT_ID") or None))
    url, path = os.getenv("MEM0_QDRANT_URL"), os.getenv("MEM0_QDRANT_PATH")
    if not (url or path):
        return None
    sf_key = os.getenv("SILICONFLOW_API_KEY")
    if not sf_key:
        raise ValueError("self-hosted mem0 embeds with SiliconFlow: set SILICONFLOW_API_KEY")
    base = os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.com/v1"
    model = os.getenv("KNOWLEDGE_EMBED_MODEL") or "Qwen/Qwen3-Embedding-0.6B"
    qdrant: dict[str, Any] = {"collection_name": os.getenv("MEM0_COLLECTION") or "keelchat_memory", "embedding_model_dims": 1024}
    qdrant |= {"url": url, "api_key": os.getenv("MEM0_QDRANT_API_KEY") or None} if url else {"path": path, "on_disk": True}
    config = {
        "vector_store": {"provider": "qdrant", "config": qdrant},
        "embedder": {"provider": "openai", "config": {"model": model, "api_key": sf_key, "openai_base_url": base, "embedding_dims": 1024}},
        # never used to infer memories (infer=False); mem0 still builds one, so it points at the same provider
        "llm": {
            "provider": "openai",
            "config": {
                "model": os.getenv("KEELCHAT_MODEL_CLASSIFY", "").removeprefix("siliconflow/") or "Qwen/Qwen3.5-35B-A3B",
                "api_key": sf_key,
                "openai_base_url": base,
            },
        },
        "history_db_path": os.getenv("MEM0_HISTORY_DB") or os.path.join(path or ".", "mem0_history.db"),
    }
    log.info("memory in mem0 (self-hosted, qdrant)", qdrant=url or path)
    return Mem0MemoryStore(LocalMem0(config))

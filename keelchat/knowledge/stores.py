"""Vector stores beyond the built-in memory and LanceDB ones: Qdrant, Chroma and Postgres with pgvector.

Each implements `VectorIndex`, keeps one collection or table per embedder (`VectorStoreConfig.table`), and filters every
search to one dataset. Their libraries are optional installs: `pip install "keelchat[qdrant]"`, `"keelchat[chroma]"`;
pgvector uses asyncpg (`keelchat[postgres]`) and a Postgres with the `vector` extension. Pick one with
KNOWLEDGE_VECTOR_STORE=qdrant|chroma|pgvector. Check your own store with `keelchat.testing.check_vector_store`.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from typing import Any

from keelchat.knowledge.models import Chunk, Hit
from keelchat.knowledge.vectorstore import VectorIndex, VectorStoreConfig, vector_store
from keelchat.logging import get_logger

log = get_logger("vector_stores")
#: pgvector's HNSW index takes vectors of up to this many dimensions
HNSW_MAX_DIM = 2000

#: chunk fields kept beside each vector, so a search returns whole chunks
_FIELDS = ("id", "doc_id", "title", "text", "context", "level", "strategy", "source", "dataset")


def _payload(c: Chunk) -> dict[str, Any]:
    return {f: getattr(c, f) for f in _FIELDS}


def _chunk(p: dict[str, Any]) -> Chunk:
    return Chunk(**{f: p[f] for f in _FIELDS if f in p})


def _local(kind: str, server_var: str, uri: str | None) -> str:
    """A local folder for an embedded store, or in memory. An object-store address (s3://...) is refused: these stores
    can't keep their files there, and a folder named after it would be lost on redeploy."""
    if not uri:
        return ":memory:"
    if "://" in uri:
        where = uri.split("?")[0]
        raise ValueError(
            f"KNOWLEDGE_VECTOR_STORE={kind} can't keep its index at {where}: set {server_var} to a {kind} server, or KNOWLEDGE_INDEX_URI to a local folder"
        )
    return uri


def _point_id(chunk_id: str) -> str:
    """Stores that want UUID ids get a stable one per chunk id."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, chunk_id))


# ------------------------------------------------------------------------------------------------------------ Qdrant


class QdrantIndex(VectorIndex):
    """Qdrant (https://qdrant.tech): a server (`url`, `api_key`), or `":memory:"` / a local path for development."""

    def __init__(self, collection: str, location: str = ":memory:", api_key: str | None = None):
        try:
            from qdrant_client import AsyncQdrantClient
        except ImportError as e:
            raise RuntimeError('KNOWLEDGE_VECTOR_STORE=qdrant needs `pip install "keelchat[qdrant]"`') from e
        remote = location.startswith(("http://", "https://"))
        kw: dict[str, Any] = {"url": location, "api_key": api_key} if remote else ({"location": location} if location == ":memory:" else {"path": location})
        self.client, self.collection, self.remote = AsyncQdrantClient(**kw), collection, remote
        self._ready = False

    async def _ensure(self, dim: int) -> None:
        from qdrant_client import models

        if self._ready:
            return
        if not await self.client.collection_exists(self.collection):
            await self.client.create_collection(self.collection, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
            if self.remote:  # filter indexes; local (in-memory or on-disk) Qdrant has none
                for field, schema in (
                    ("dataset", models.PayloadSchemaType.KEYWORD),
                    ("doc_id", models.PayloadSchemaType.KEYWORD),
                    ("level", models.PayloadSchemaType.INTEGER),
                ):
                    await self.client.create_payload_index(self.collection, field, field_schema=schema)
        self._ready = True

    def _filter(self, dataset: str | None, max_level: int | None = None, doc_id: str | None = None) -> Any:
        from qdrant_client import models

        must: list[Any] = []
        if dataset is not None:
            must.append(models.FieldCondition(key="dataset", match=models.MatchValue(value=dataset)))
        if max_level is not None:
            must.append(models.FieldCondition(key="level", range=models.Range(lte=max_level)))
        if doc_id is not None:
            must.append(models.FieldCondition(key="doc_id", match=models.MatchValue(value=doc_id)))
        return models.Filter(must=must) if must else None

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        from qdrant_client import models

        if not chunks:
            return
        await self._ensure(len(vectors[0]))
        points = [models.PointStruct(id=_point_id(c.id), vector=v, payload=_payload(c)) for c, v in zip(chunks, vectors, strict=True)]
        await self.client.upsert(self.collection, points=points, wait=True)

    async def search(self, vector: list[float], k: int = 5, max_level: int | None = None, dataset: str = "") -> list[Hit]:
        if not await self.client.collection_exists(self.collection):
            return []
        res = await self.client.query_points(self.collection, query=vector, query_filter=self._filter(dataset, max_level), limit=k, with_payload=True)
        return [Hit(chunk=_chunk(dict(p.payload or {})), score=float(p.score)) for p in res.points]

    async def delete_doc(self, doc_id: str) -> None:
        from qdrant_client import models

        if await self.client.collection_exists(self.collection):
            await self.client.delete(self.collection, points_selector=models.FilterSelector(filter=self._filter(None, doc_id=doc_id)), wait=True)

    async def count(self, dataset: str | None = None) -> int:
        if not await self.client.collection_exists(self.collection):
            return 0
        return int((await self.client.count(self.collection, count_filter=self._filter(dataset), exact=True)).count)

    async def doc_ids(self, dataset: str) -> set[str] | None:
        if not await self.client.collection_exists(self.collection):
            return set()
        out: set[str] = set()
        offset: Any = None
        while True:
            points, offset = await self.client.scroll(
                self.collection, scroll_filter=self._filter(dataset), limit=1000, offset=offset, with_payload=["doc_id"], with_vectors=False
            )
            out |= {str((p.payload or {}).get("doc_id", "")) for p in points}
            if offset is None:
                return out


@vector_store("qdrant")
def _qdrant(cfg: VectorStoreConfig) -> VectorIndex:
    """QDRANT_URL (+ QDRANT_API_KEY) for a server; else a local path in KNOWLEDGE_INDEX_URI; else in memory."""
    return QdrantIndex(cfg.table, os.getenv("QDRANT_URL") or _local("qdrant", "QDRANT_URL", cfg.uri), os.getenv("QDRANT_API_KEY"))


# ------------------------------------------------------------------------------------------------------------ Chroma


class ChromaIndex(VectorIndex):
    """Chroma (https://www.trychroma.com): a server (`host`), a local folder, or in memory. Its client is synchronous,
    so every call runs in a thread."""

    def __init__(self, collection: str, location: str = ":memory:"):
        try:
            import chromadb
        except ImportError as e:
            raise RuntimeError('KNOWLEDGE_VECTOR_STORE=chroma needs `pip install "keelchat[chroma]"`') from e
        if location.startswith(("http://", "https://")):
            m = re.match(r"https?://([^:/]+)(?::(\d+))?", location)
            assert m is not None
            client = chromadb.HttpClient(host=m.group(1), port=int(m.group(2) or 8000), ssl=location.startswith("https"))
        elif location == ":memory:":
            client = chromadb.EphemeralClient()
        else:
            client = chromadb.PersistentClient(path=location)
        self.col = client.get_or_create_collection(collection, metadata={"hnsw:space": "cosine"}, embedding_function=None)

    @staticmethod
    def _where(dataset: str | None, max_level: int | None = None) -> dict[str, Any] | None:
        parts: list[dict[str, Any]] = []
        if dataset is not None:
            parts.append({"dataset": dataset})
        if max_level is not None:
            parts.append({"level": {"$lte": max_level}})
        return None if not parts else parts[0] if len(parts) == 1 else {"$and": parts}

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        await asyncio.to_thread(
            self.col.upsert,
            ids=[c.id for c in chunks],
            embeddings=list[Any](vectors),
            documents=[c.text for c in chunks],
            metadatas=[{k: v for k, v in _payload(c).items() if k not in ("id", "text")} for c in chunks],
        )

    async def search(self, vector: list[float], k: int = 5, max_level: int | None = None, dataset: str = "") -> list[Hit]:
        def _do() -> list[Hit]:
            n = self.col.count()
            if n == 0:
                return []
            r = self.col.query(
                query_embeddings=list[Any]([vector]),
                n_results=min(k, n),
                where=self._where(dataset, max_level),
                include=["metadatas", "documents", "distances"],
            )
            ids, metas, docs, dists = r["ids"][0], (r["metadatas"] or [[]])[0], (r["documents"] or [[]])[0], (r["distances"] or [[]])[0]
            return [Hit(chunk=_chunk({**dict(m), "id": i, "text": d}), score=1.0 - float(s)) for i, m, d, s in zip(ids, metas, docs, dists, strict=True)]

        return await asyncio.to_thread(_do)

    async def delete_doc(self, doc_id: str) -> None:
        await asyncio.to_thread(self.col.delete, where={"doc_id": doc_id})

    async def count(self, dataset: str | None = None) -> int:
        if dataset is None:
            return int(await asyncio.to_thread(self.col.count))
        got = await asyncio.to_thread(self.col.get, where={"dataset": dataset}, include=[])
        return len(got["ids"])

    async def doc_ids(self, dataset: str) -> set[str] | None:
        got = await asyncio.to_thread(self.col.get, where={"dataset": dataset}, include=["metadatas"])
        return {str(m.get("doc_id", "")) for m in got["metadatas"] or []}


@vector_store("chroma")
def _chroma(cfg: VectorStoreConfig) -> VectorIndex:
    """CHROMA_URL for a server; else a local folder in KNOWLEDGE_INDEX_URI; else in memory."""
    return ChromaIndex(cfg.table, os.getenv("CHROMA_URL") or _local("chroma", "CHROMA_URL", cfg.uri))


# ---------------------------------------------------------------------------------------------------------- pgvector


class PgVectorIndex(VectorIndex):
    """Postgres with the pgvector extension: one table per embedder, cosine distance, an HNSW index. The table is
    created on the first write (when the vector size is known)."""

    def __init__(self, dsn: str, table: str):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", table):
            raise ValueError(f"pgvector table name {table!r} must be lowercase letters, digits and _")
        self.dsn, self.table = dsn, table
        self._pool: Any = None
        self._ready = False
        self._lock = asyncio.Lock()

    async def _db(self) -> Any:
        if self._pool is None:
            import asyncpg

            self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=4)
        return self._pool

    async def _exists(self) -> bool:
        if self._ready:
            return True
        pool = await self._db()
        self._ready = bool(await pool.fetchval("SELECT to_regclass($1) IS NOT NULL", self.table))
        return self._ready

    async def _ensure(self, dim: int) -> None:
        async with self._lock:
            if await self._exists():
                return
            pool = await self._db()
            async with pool.acquire() as con, con.transaction():
                await con.execute("SELECT pg_advisory_xact_lock(hashtext($1))", self.table)  # one creator at a time
                await con.execute("CREATE EXTENSION IF NOT EXISTS vector")
                await con.execute(
                    f"CREATE TABLE IF NOT EXISTS {self.table} (id text PRIMARY KEY, doc_id text NOT NULL, title text, text text,"
                    f" context text, level int NOT NULL DEFAULT 0, strategy text, source text, dataset text NOT NULL DEFAULT '',"
                    f" embedding vector({int(dim)}) NOT NULL)"
                )
                await con.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_ds ON {self.table} (dataset)")
                await con.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_doc ON {self.table} (doc_id)")
                if dim <= HNSW_MAX_DIM:
                    await con.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_hnsw ON {self.table} USING hnsw (embedding vector_cosine_ops)")
                else:  # pgvector indexes vectors of up to 2000 dimensions: larger ones are searched exactly
                    log.warning("pgvector: no vector index for this embedding size; searches scan the dataset", table=self.table, dim=dim)
            self._ready = True

    @staticmethod
    def _vec(v: list[float]) -> str:
        return json.dumps([float(x) for x in v])

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        if not chunks:
            return
        await self._ensure(len(vectors[0]))
        rows = [
            (c.id, c.doc_id, c.title, c.text, c.context, c.level, c.strategy, c.source, c.dataset, self._vec(v)) for c, v in zip(chunks, vectors, strict=True)
        ]
        pool = await self._db()
        await pool.executemany(
            f"INSERT INTO {self.table} (id, doc_id, title, text, context, level, strategy, source, dataset, embedding)"
            f" VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::vector) ON CONFLICT (id) DO UPDATE SET doc_id = EXCLUDED.doc_id,"
            f" title = EXCLUDED.title, text = EXCLUDED.text, context = EXCLUDED.context, level = EXCLUDED.level,"
            f" strategy = EXCLUDED.strategy, source = EXCLUDED.source, dataset = EXCLUDED.dataset, embedding = EXCLUDED.embedding",
            rows,
        )

    async def search(self, vector: list[float], k: int = 5, max_level: int | None = None, dataset: str = "") -> list[Hit]:
        if not await self._exists():
            return []
        if not any(vector):
            return []  # a zero vector has no direction: cosine is undefined
        pool = await self._db()
        sql = (
            "SELECT * FROM (SELECT id, doc_id, title, text, context, level, strategy, source, dataset,"
            f" embedding <=> $1::vector AS distance FROM {self.table} WHERE dataset = $2 AND ($3::int IS NULL OR level <= $3)"
            " ORDER BY embedding <=> $1::vector LIMIT $4) hits ORDER BY distance"
        )
        async with pool.acquire() as con, con.transaction():
            # the HNSW index is read first and the dataset filtered after: keep scanning until enough rows match
            # (pgvector 0.8+), else widen the search, so a small dataset is not hidden by a large one
            try:
                async with con.transaction():
                    await con.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            except Exception:  # older pgvector: no iterative scans
                await con.execute("SET LOCAL hnsw.ef_search = 1000")
            rows = await con.fetch(sql, self._vec(vector), dataset, max_level, k)
        return [Hit(chunk=_chunk(dict(r)), score=1.0 - float(r["distance"])) for r in rows]

    async def doc_ids(self, dataset: str) -> set[str] | None:
        if not await self._exists():
            return set()
        return {r["doc_id"] for r in await (await self._db()).fetch(f"SELECT DISTINCT doc_id FROM {self.table} WHERE dataset = $1", dataset)}

    async def delete_doc(self, doc_id: str) -> None:
        if await self._exists():
            await (await self._db()).execute(f"DELETE FROM {self.table} WHERE doc_id = $1", doc_id)

    async def count(self, dataset: str | None = None) -> int:
        if not await self._exists():
            return 0
        pool = await self._db()
        if dataset is None:
            return int(await pool.fetchval(f"SELECT count(*) FROM {self.table}"))
        return int(await pool.fetchval(f"SELECT count(*) FROM {self.table} WHERE dataset = $1", dataset))


@vector_store("pgvector")
def _pgvector(cfg: VectorStoreConfig) -> VectorIndex:
    """KNOWLEDGE_PGVECTOR_URL, else DATABASE_URL: a Postgres with the vector extension available."""
    dsn = os.getenv("KNOWLEDGE_PGVECTOR_URL") or os.getenv("DATABASE_URL")
    if not dsn:
        raise ValueError("KNOWLEDGE_VECTOR_STORE=pgvector needs KNOWLEDGE_PGVECTOR_URL or DATABASE_URL")
    return PgVectorIndex(dsn, cfg.table)

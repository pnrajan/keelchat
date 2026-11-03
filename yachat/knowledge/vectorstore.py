"""Vector index port: LanceDB (local path or s3:// URI, shared by every Knowledge Service server) and an in-memory
cosine fake."""

from __future__ import annotations

import asyncio
import math
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from yachat.knowledge.models import Chunk, Hit


class VectorIndex(ABC):
    @abstractmethod
    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None: ...
    @abstractmethod
    async def search(self, vector: list[float], k: int = 5, max_level: int | None = None, dataset: str = "") -> list[Hit]:
        """Only chunks of `dataset` ("" = the shared corpus): a search never crosses into another dataset."""

    @abstractmethod
    async def delete_doc(self, doc_id: str) -> None: ...
    @abstractmethod
    async def count(self, dataset: str | None = None) -> int:
        """Chunks in every scope, or in one ("" = the shared corpus)."""


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return dot / (na * nb)


class InMemoryIndex(VectorIndex):
    def __init__(self) -> None:
        self._rows: dict[str, tuple[Chunk, list[float]]] = {}

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        for c, v in zip(chunks, vectors, strict=True):
            self._rows[c.id] = (c, v)

    async def search(self, vector: list[float], k: int = 5, max_level: int | None = None, dataset: str = "") -> list[Hit]:
        hits = [Hit(chunk=c, score=cosine(vector, v)) for c, v in self._rows.values() if c.dataset == dataset and (max_level is None or c.level <= max_level)]
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]

    async def delete_doc(self, doc_id: str) -> None:
        for cid in [cid for cid, (c, _) in self._rows.items() if c.doc_id == doc_id]:
            del self._rows[cid]

    async def count(self, dataset: str | None = None) -> int:
        return sum(1 for c, _ in self._rows.values() if dataset is None or c.dataset == dataset)


def _quote(value: str) -> str:
    return value.replace("'", "''")


#: Lance commits to S3 with conditional writes, so servers sharing one index never lose a commit: a writer that loses
#: a race re-applies its change, and gives up with this error after its own retries. We try the commit again a few times.
_CONFLICT = "Too many concurrent writers"


def _committed(commit: Callable[[], Any], attempts: int = 4) -> None:
    for attempt in range(attempts):
        try:
            commit()
            return
        except RuntimeError as e:
            if _CONFLICT not in str(e) or attempt == attempts - 1:
                raise
            time.sleep(random.uniform(0.5, 2.0) * (attempt + 1))


class LanceDBIndex(VectorIndex):
    """LanceDB table at `uri` (a local directory or s3://bucket/path). Sync client, run in a thread."""

    def __init__(self, uri: str, table: str = "chunks", dim: int = 1024, consistency_s: float = 5.0):
        """`consistency_s`: how stale a read may be. Other servers' changes to the shared index show up within this
        many seconds (0: every read checks for a newer version, one extra storage request)."""
        import lancedb

        self.db = lancedb.connect(uri, read_consistency_interval=timedelta(seconds=consistency_s))
        self.table_name, self.dim = table, dim
        self._table: Any = None

    def _row(self, c: Chunk, v: list[float]) -> dict[str, Any]:
        return {
            "id": c.id,
            "doc_id": c.doc_id,
            "title": c.title,
            "text": c.text,
            "context": c.context,
            "level": c.level,
            "strategy": c.strategy,
            "source": c.source,
            "dataset": c.dataset,
            "vector": v,
        }

    def _ensure(self, rows: list[dict[str, Any]] | None = None) -> Any:
        if self._table is None:
            if self.table_name in self.db.table_names():
                self._table = self.db.open_table(self.table_name)
                if "dataset" not in self._table.schema.names:  # indexes built before datasets: their chunks are the shared corpus
                    self._table.add_columns({"dataset": "''"})
            elif rows:
                try:
                    self._table = self.db.create_table(self.table_name, data=rows)
                    return self._table, True
                except (ValueError, OSError):  # another writer created it first: use theirs, add our rows to it
                    self._table = self.db.open_table(self.table_name)
        return self._table, False

    async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
        rows = [self._row(c, v) for c, v in zip(chunks, vectors, strict=True)]
        if not rows:
            return

        def _do() -> None:
            table, created = self._ensure(rows)
            if not created:  # one commit: replace chunks with these ids, add the rest
                _committed(lambda: table.merge_insert("id").when_matched_update_all().when_not_matched_insert_all().execute(rows))

        await asyncio.to_thread(_do)

    async def search(self, vector: list[float], k: int = 5, max_level: int | None = None, dataset: str = "") -> list[Hit]:
        def _do() -> list[Hit]:
            table, _ = self._ensure()
            if table is None:
                return []
            where = f"dataset = '{_quote(dataset)}'" + (f" AND level <= {int(max_level)}" if max_level is not None else "")
            q = table.search(vector).metric("cosine").where(where, prefilter=True).limit(k)
            out = []
            for r in q.to_list():
                chunk = Chunk(
                    id=r["id"],
                    doc_id=r["doc_id"],
                    title=r["title"],
                    text=r["text"],
                    context=r.get("context", ""),
                    level=int(r["level"]),
                    strategy=r.get("strategy", "simple"),
                    source=r.get("source", ""),
                    dataset=r.get("dataset", ""),
                )
                out.append(Hit(chunk=chunk, score=1.0 - float(r.get("_distance", 0.0))))
            return out

        return await asyncio.to_thread(_do)

    async def delete_doc(self, doc_id: str) -> None:
        def _do() -> None:
            table, _ = self._ensure()
            if table is not None:
                _committed(lambda: table.delete(f"doc_id = '{_quote(doc_id)}'"))

        await asyncio.to_thread(_do)

    async def count(self, dataset: str | None = None) -> int:
        def _do() -> int:
            table, _ = self._ensure()
            if table is None:
                return 0
            return int(table.count_rows() if dataset is None else table.count_rows(f"dataset = '{_quote(dataset)}'"))

        return await asyncio.to_thread(_do)  # S3 I/O off the event loop

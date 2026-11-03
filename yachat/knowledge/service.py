"""KnowledgeService: index documents with a strategy, search, and expose a `knowledge_search` tool implementation."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from yachat.datasets.models import Dataset, UnstructuredDataset
from yachat.datasets.sources import read_bytes
from yachat.knowledge.chunker import SemanticChunker
from yachat.knowledge.docstore import DocumentStore, InMemoryDocumentStore
from yachat.knowledge.embedder import Embedder, FakeEmbedder
from yachat.knowledge.models import Document, Hit, Passage, Strategy
from yachat.knowledge.strategies import ContextualIndexer, FullContextIndexer, Indexer, RaptorIndexer, SimpleIndexer
from yachat.knowledge.vectorstore import InMemoryIndex, VectorIndex
from yachat.logging import get_logger
from yachat.runtime.llm import LLMClient, ModelConfig

log = get_logger("knowledge")


def make_indexer(strategy: Strategy, embedder: Embedder, llm: LLMClient | None = None, models: ModelConfig | None = None) -> Indexer:
    """The embedder also drives chunk boundaries (SemanticChunker); contextual and raptor need a model as well."""
    models = models or ModelConfig()
    chunker = SemanticChunker(embedder)
    if strategy == "simple":
        return SimpleIndexer(chunker)
    if strategy == "full_context":
        return FullContextIndexer()
    if llm is None:
        raise ValueError(f"strategy {strategy!r} needs an LLMClient")
    if strategy == "contextual":
        return ContextualIndexer(llm, models, chunker)
    if strategy == "raptor":
        return RaptorIndexer(llm, models, chunker)
    raise ValueError(f"unknown strategy {strategy!r}")


class KnowledgeService:
    def __init__(self, docs: DocumentStore, index: VectorIndex, embedder: Embedder, indexer: Indexer, *, full_context_budget_chars: int = 40_000):
        self.docs, self.index, self.embedder, self.indexer = docs, index, embedder, indexer
        self.full_context_budget_chars = full_context_budget_chars
        #: dataset ref → version indexed by this process; a miss (new version, restart) re-indexes once
        self._indexed: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: dataset ref → the version dropped: a search still holding that version never re-indexes it (a new one would)
        self._dropped: dict[str, str] = {}
        self._doc_count: tuple[float, int] | None = None

    @property
    def strategy(self) -> Strategy:
        return self.indexer.name

    async def add(self, doc: Document) -> int:
        n = await self._index(doc)  # vectors first: a stored document always has its vectors (an unchanged one is skipped)
        await self.docs.put(doc)
        self._doc_count = None
        return n

    async def reindex_if_empty(self) -> int:
        """Stored documents outlive an in-memory index (S3 documents, no KNOWLEDGE_INDEX_URI): rebuild it at startup."""
        if self.strategy == "full_context" or await self.index.count(dataset="") > 0:  # the shared corpus only
            return 0
        docs = await self.docs.list("")  # datasets re-index on first use, from their sources
        n = sum([await self._index(d) for d in docs])
        if docs:
            log.info("index rebuilt from stored documents", documents=len(docs), chunks=n)
        return n

    async def _index(self, doc: Document) -> int:
        chunks = [c.model_copy(update={"dataset": doc.dataset}) for c in await self.indexer.chunks(doc)]
        if chunks:
            vectors = await self.embedder.embed([c.embed_text for c in chunks])
            await self.index.delete_doc(doc.id)
            await self.index.upsert(chunks, vectors)
        log.info("document indexed", doc_id=doc.id, strategy=self.strategy, chunks=len(chunks))
        return len(chunks)

    async def index_dataset(self, dataset: Dataset, *, register: bool = False) -> int:
        """(Re)index an unstructured dataset's documents; documents no longer listed are removed. Idempotent per version.
        `register`: an explicit (re-)registration, which lifts a drop of the same version; a search never does."""
        if not isinstance(dataset.spec, UnstructuredDataset):
            raise ValueError(f"{dataset.ref} is a {dataset.kind} dataset; the Knowledge Service indexes unstructured ones")
        async with self._locks.setdefault(dataset.ref, asyncio.Lock()):
            if register:
                self._dropped.pop(dataset.ref, None)
            if self._indexed.get(dataset.ref) == dataset.version or self._dropped.get(dataset.ref) == dataset.version:
                return 0
            docs = []
            for src in dataset.spec.documents:
                text = (await asyncio.to_thread(read_bytes, src.uri)).decode("utf-8", errors="replace")
                title = src.title or src.uri.rsplit("/", 1)[-1]
                docs.append(Document.create(title, text, src.uri, metadata={"dataset_version": dataset.version}, dataset=dataset.ref))
            keep = {d.id for d in docs}
            stored = await self.docs.ids(dataset.ref)  # names only: nothing is downloaded
            for old in stored - keep:
                await self.remove(old, dataset.ref)
            # ids come from the content: a document already stored is unchanged, so it is not embedded again (a
            # restart, another replica, or a new version that only changed the description)
            n = sum([await self.add(d) for d in docs if d.id not in stored])
            self._indexed[dataset.ref] = dataset.version
            log.info("dataset indexed", dataset=dataset.ref, version=dataset.version, documents=len(docs), chunks=n)
            return n

    async def drop_dataset(self, ref: str, version: str | None = None) -> int:
        """Remove a dataset's documents and vectors. Waits for an indexing run of the dataset, so it cannot add them
        back afterwards; `version` (the deleted one) is never indexed again by this process."""
        async with self._locks.setdefault(ref, asyncio.Lock()):
            dropped = version or self._indexed.get(ref)  # read before remove() clears it
            removed = sorted(await self.docs.ids(ref))
            for doc_id in removed:
                await self.remove(doc_id, ref)
            self._indexed.pop(ref, None)
            if dropped:
                self._dropped[ref] = dropped
            log.info("dataset dropped", dataset=ref, documents=len(removed))
            return len(removed)

    async def document_count(self) -> int:
        """For /health, polled often: counted once a minute at most (object names only, never their contents)."""
        now = time.monotonic()
        if self._doc_count is None or now - self._doc_count[0] > 60:
            self._doc_count = (now, await self.docs.count())
        return self._doc_count[1]

    async def remove(self, doc_id: str, dataset: str = "") -> None:
        """Delete a document and its chunks. Removing one of a dataset's documents also marks the dataset for
        re-indexing, so its next search restores what its sources still list."""
        self._doc_count = None
        await self.docs.delete(doc_id, dataset)
        await self.index.delete_doc(doc_id)
        if dataset:
            self._indexed.pop(dataset, None)

    async def search(self, query: str, k: int = 5, dataset: Dataset | None = None) -> list[Passage]:
        """Search the shared corpus, or one dataset (indexed first if this process has not indexed its version)."""
        ref = ""
        if dataset is not None:
            await self.index_dataset(dataset)
            ref = dataset.ref
        if self.strategy == "full_context":
            return await self._full_context(query, ref)
        [qv] = await self.embedder.embed([query])
        hits: list[Hit] = await self.index.search(qv, k=k, dataset=ref)
        return [
            Passage(
                title=h.chunk.title,
                text=(f"{h.chunk.context}\n{h.chunk.text}" if h.chunk.context and h.chunk.level == 0 else h.chunk.text),
                source=h.chunk.source,
                score=round(h.score, 4),
            )
            for h in hits
        ]

    async def _full_context(self, query: str, dataset: str = "") -> list[Passage]:
        """Whole documents, most lexically relevant first, until the prompt budget is spent."""
        q = {w for w in query.lower().split() if len(w) > 2}
        docs = await self.docs.list(dataset)
        scored = sorted(docs, key=lambda d: -len(q & set(d.text.lower().split())))
        out, used = [], 0
        for d in scored:
            if used + len(d.text) > self.full_context_budget_chars:
                continue
            used += len(d.text)
            out.append(Passage(title=d.title, text=d.text, source=d.source, score=float(len(q & set(d.text.lower().split())))))
        return out

    def tool_impl(self) -> Callable[..., Awaitable[list[dict[str, Any]]]]:
        """A `knowledge_search` implementation for Runtime(tool_impls=...)."""

        async def knowledge_search(query: str, dataset: Dataset | None = None) -> list[dict[str, Any]]:
            return [p.model_dump() for p in await self.search(query, dataset=dataset)]

        return knowledge_search


def index_uri(bucket: str | None, docs_prefix: str) -> str | None:
    """KNOWLEDGE_INDEX_URI, else beside the documents in the knowledge bucket (`knowledge/docs/` → `knowledge/index`),
    so every Knowledge Service server shares one index. None: in memory, rebuilt from the documents at startup."""
    if uri := os.getenv("KNOWLEDGE_INDEX_URI"):
        return uri
    if not bucket:
        return None
    parent = docs_prefix.strip("/").rpartition("/")[0]
    return f"s3://{bucket}/{parent + '/' if parent else ''}index"


def service_from_env(llm: LLMClient | None = None, models: ModelConfig | None = None) -> KnowledgeService:
    """Wire from environment. Fakes when nothing is configured; a configured real adapter with a missing key raises."""
    strategy: Strategy = os.getenv("KNOWLEDGE_STRATEGY") or "simple"  # type: ignore[assignment]  # blank in .env = default
    docs: DocumentStore = InMemoryDocumentStore()
    bucket, prefix = os.getenv("KNOWLEDGE_S3_BUCKET"), os.getenv("KNOWLEDGE_S3_PREFIX", "knowledge/docs/")
    if bucket:
        from yachat.knowledge.docstore import ArtifactDocumentStore
        from yachat.state.artifacts import S3ArtifactStore

        docs = ArtifactDocumentStore(S3ArtifactStore(bucket), prefix=prefix)
    embedder: Embedder = FakeEmbedder()
    if os.getenv("SILICONFLOW_API_KEY"):
        from yachat.knowledge.embedder import SiliconFlowEmbedder

        embedder = SiliconFlowEmbedder()
    index: VectorIndex = InMemoryIndex()
    if uri := index_uri(bucket, prefix):
        from yachat.knowledge.vectorstore import LanceDBIndex

        index = LanceDBIndex(uri, dim=embedder.dim)
        log.info("knowledge index", index_uri=uri.split("?")[0])
    return KnowledgeService(docs, index, embedder, make_indexer(strategy, embedder, llm, models))

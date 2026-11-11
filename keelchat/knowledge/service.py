"""KnowledgeService: index documents with a strategy, search, and expose a `knowledge_search` tool implementation."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

from keelchat.datasets.models import Dataset, UnstructuredDataset
from keelchat.datasets.sources import read_bytes
from keelchat.knowledge.chunker import Chunker, SemanticChunker, chunker_from_env
from keelchat.knowledge.docstore import DocumentStore, InMemoryDocumentStore
from keelchat.knowledge.embedder import Embedder, embedder_from_env, index_table
from keelchat.knowledge.models import Document, Hit, Passage, Strategy
from keelchat.knowledge.plugins import plugin_name
from keelchat.knowledge.strategies import IndexContext, Indexer, indexers
from keelchat.knowledge.vectorstore import VectorIndex, VectorStoreConfig, vector_stores
from keelchat.logging import get_logger
from keelchat.runtime.llm import LLMClient, ModelConfig

log = get_logger("knowledge")


def make_indexer(
    strategy: Strategy, embedder: Embedder, llm: LLMClient | None = None, models: ModelConfig | None = None, chunker: Chunker | None = None
) -> Indexer:
    """The indexing method registered as `strategy` (built-in or @indexer), known by that name. The chunker defaults to
    the semantic one (the embedder drives the breaks); contextual and raptor need a model as well."""
    ctx = IndexContext(embedder, chunker or SemanticChunker(embedder), llm, models or ModelConfig())
    made = indexers.make(strategy, ctx)
    if getattr(made, "name", None) != strategy:
        try:
            made.name = strategy  # one name everywhere: chunks, logs, the method a knowledge base reports
        except AttributeError:
            raise ValueError(f"strategy {strategy!r} made an indexer named {made.name!r} that cannot be renamed") from None
    return made


#: a document this long (characters) is better served by RAPTOR's layered summaries than by passages alone
LONG_DOC_CHARS = 30_000


def choose_strategy(texts: list[str], budget_chars: int, can_summarize: bool) -> tuple[Strategy, str]:
    """The indexing method for a knowledge base, from its documents: the reason is shown to the person who made it."""
    total, longest, n = sum(map(len, texts)), max(map(len, texts), default=0), len(texts)
    docs = f"{n} document{'' if n == 1 else 's'}"
    if total <= budget_chars:
        whole = "the document fits in one prompt, so the bot reads it" if n == 1 else f"the {docs} fit in one prompt, so the bot reads them"
        return "full_context", f"Full context: {whole} whole."
    if not can_summarize:
        return "simple", f"Search: the {docs} are split into passages and the best ones are found by meaning."
    if longest > LONG_DOC_CHARS:
        return "raptor", "RAPTOR: long documents get summaries at several levels, for broad and detailed questions alike."
    return "contextual", f"Contextual retrieval: the {docs} are split into passages, each with a line of context, so search finds the right one."


class KnowledgeService:
    def __init__(
        self,
        docs: DocumentStore,
        index: VectorIndex,
        embedder: Embedder,
        indexer: Indexer,
        *,
        full_context_budget_chars: int = 40_000,
        llm: LLMClient | None = None,
        models: ModelConfig | None = None,
        auto: bool = False,
        chunker: Chunker | None = None,
    ):
        self.docs, self.index, self.embedder, self.indexer = docs, index, embedder, indexer
        self.full_context_budget_chars = full_context_budget_chars
        #: contextual and raptor summarize with a model; without one, "auto" picks among the others
        self.llm, self.models = llm, models
        #: "auto" knowledge bases pick their method; otherwise they use this service's (KNOWLEDGE_STRATEGY)
        self.auto = auto
        self.chunker = chunker or SemanticChunker(embedder)  # what further indexing methods split documents with
        self._indexers: dict[str, Indexer] = {indexer.name: indexer}
        #: dataset ref → (method, why), as last indexed by this process
        self._methods: dict[str, tuple[Strategy, str]] = {}
        #: dataset ref → version indexed by this process; a miss (new version, restart) re-indexes once
        self._indexed: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        #: dataset ref → the version dropped: a search still holding that version never re-indexes it (a new one would)
        self._dropped: dict[str, str] = {}
        self._doc_count: tuple[float, int] | None = None

    @property
    def strategy(self) -> Strategy:
        return self.indexer.name

    def _indexer(self, strategy: Strategy) -> Indexer:
        if strategy not in self._indexers:
            self._indexers[strategy] = make_indexer(strategy, self.embedder, self.llm, self.models, self.chunker)
        return self._indexers[strategy]

    def method(self, ref: str) -> tuple[Strategy, str] | None:
        """How a dataset was indexed, and why; None until this process has indexed it."""
        return self._methods.get(ref)

    async def add(self, doc: Document, indexer: Indexer | None = None) -> int:
        n = await self._index(doc, indexer)  # vectors first: a stored document always has its vectors (an unchanged one is skipped)
        await self.docs.put(doc)
        self._doc_count = None
        return n

    async def reindex_if_empty(self) -> int:
        """Stored documents outlive an in-memory index (S3 documents, no KNOWLEDGE_INDEX_URI): rebuild it at startup."""
        if self.indexer.retrieval == "whole_documents" or await self.index.count(dataset="") > 0:  # the shared corpus only
            return 0
        docs = await self.docs.list("")  # datasets re-index on first use, from their sources
        n = sum([await self._index(d) for d in docs])
        if docs:
            log.info("index rebuilt from stored documents", documents=len(docs), chunks=n)
        return n

    async def _index(self, doc: Document, indexer: Indexer | None = None) -> int:
        indexer = indexer or self.indexer
        chunks = [c.model_copy(update={"dataset": doc.dataset}) for c in await indexer.chunks(doc)]
        if chunks:
            vectors = await self.embedder.embed([c.embed_text for c in chunks])
            await self.index.delete_doc(doc.id)
            await self.index.upsert(chunks, vectors)
        log.info("document indexed", doc_id=doc.id, strategy=indexer.name, chunks=len(chunks))
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
            spec = dataset.spec
            if spec.strategy == "auto" and not self.auto:
                strategy, why = self.strategy, "The Knowledge Service's method (KNOWLEDGE_STRATEGY)."
            elif spec.strategy == "auto":
                strategy, why = choose_strategy([d.text for d in docs], self.full_context_budget_chars, self.llm is not None)
            else:
                strategy, why = spec.strategy, "Chosen when the knowledge base was made."
            indexer = self._indexer(strategy)  # before anything is removed: an unknown method changes nothing
            keep = {d.id for d in docs}
            stored = await self.docs.ids(dataset.ref)  # names only: nothing is downloaded
            prev = self._methods.get(dataset.ref)
            for old in stored if prev and prev[0] != strategy else stored - keep:  # a new method re-indexes everything
                await self.remove(old, dataset.ref)
            stored = await self.docs.ids(dataset.ref)
            # ids come from the content: a document already stored is unchanged, so it is not embedded again (a
            # restart, another replica, or a new version that only changed the description)
            n = sum([await (self.add(d) if indexer is self.indexer else self.add(d, indexer)) for d in docs if d.id not in stored])
            self._indexed[dataset.ref] = dataset.version
            self._methods[dataset.ref] = (strategy, why)
            log.info("dataset indexed", dataset=dataset.ref, version=dataset.version, documents=len(docs), chunks=n, strategy=strategy)
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
            self._methods.pop(ref, None)
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
        ref, strategy = "", self.strategy
        if dataset is not None:
            await self.index_dataset(dataset)
            ref = dataset.ref
            strategy = self._methods[ref][0] if ref in self._methods else strategy
            if isinstance(dataset.spec, UnstructuredDataset):
                k = dataset.spec.top_k
        if self._indexer(strategy).retrieval == "whole_documents":
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

    def tool_impl(self, shared: Callable[..., Awaitable[Any]] | None = None) -> Callable[..., Awaitable[list[dict[str, Any]]]]:
        """A `knowledge_search` implementation for Runtime(tool_impls=...). `shared` answers searches that name no
        dataset (e.g. sample data); a named dataset is always searched here."""

        async def knowledge_search(query: str, dataset: Dataset | None = None) -> list[dict[str, Any]]:
            if dataset is None and shared is not None:
                return list(await shared(query=query))
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
    strategy: Strategy = os.getenv("KNOWLEDGE_STRATEGY") or "simple"  # blank in .env = default
    auto = not os.getenv("KNOWLEDGE_STRATEGY")  # unset: each knowledge base set to "auto" picks its own method
    docs: DocumentStore = InMemoryDocumentStore()
    bucket, prefix = os.getenv("KNOWLEDGE_S3_BUCKET"), os.getenv("KNOWLEDGE_S3_PREFIX", "knowledge/docs/")
    if bucket:
        from keelchat.knowledge.docstore import ArtifactDocumentStore
        from keelchat.state.artifacts import S3ArtifactStore

        docs = ArtifactDocumentStore(S3ArtifactStore(bucket), prefix=prefix)
    embedder = embedder_from_env()
    chunker = chunker_from_env(embedder)
    uri = index_uri(bucket, prefix)
    # KNOWLEDGE_VECTOR_STORE: memory, lancedb, a registered name or package.module:Name; unset, lancedb when there is
    # somewhere to keep it, else memory. Each embedder's vectors get their own table.
    store = os.getenv("KNOWLEDGE_VECTOR_STORE") or ("lancedb" if uri else "memory")
    index = vector_stores.make(store, VectorStoreConfig(uri, index_table(embedder), embedder.dim))
    log.info("knowledge index", store=store, index_uri=(uri or "").split("?")[0], embedder=embedder.id, chunker=plugin_name(chunker))
    indexer = make_indexer(strategy, embedder, llm, models, chunker)
    return KnowledgeService(docs, index, embedder, indexer, llm=llm, models=models, auto=auto, chunker=chunker)


def local_service_from_env(llm: LLMClient | None = None, models: ModelConfig | None = None) -> KnowledgeService | None:
    """The knowledge index inside the API process, when no Knowledge Service is configured (KNOWLEDGE_URL unset), so a
    single deploy can answer from documents. None when KNOWLEDGE_URL is set."""
    if os.getenv("KNOWLEDGE_URL"):
        return None
    return service_from_env(llm, models)

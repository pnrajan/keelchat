"""Embedders: built-in providers, the automatic choice, and your own through `@embedder` or an import path."""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest

from keelchat import Embedder, embedder
from keelchat.knowledge import embedder as emb
from keelchat.knowledge.embedder import EMBEDDERS, FakeEmbedder, LiteLLMEmbedder, embedder_from_env, index_table

ENV = ("KNOWLEDGE_EMBEDDER", "KNOWLEDGE_EMBED_MODEL", "KNOWLEDGE_EMBED_DIM", "SILICONFLOW_API_KEY", "OPENAI_API_KEY")


@pytest.fixture
def clean_env(monkeypatch):
    for v in ENV:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(emb, "_sentence_transformers_installed", lambda: False)
    monkeypatch.setattr(emb, "_chonkie_installed", lambda: False)
    return monkeypatch


class Letters(Embedder):
    """A tiny embedder of the kind a user would write: one dimension per letter a-z."""

    dim = 26

    def __init__(self, model: str = "letters-v1"):
        self.model = model

    @property
    def id(self) -> str:
        return f"letters:{self.model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(t.lower().count(chr(97 + i))) for i in range(26)] for t in texts]


def test_your_own_embedder_is_chosen_by_its_registered_name(clean_env):
    embedder("letters")(Letters)
    try:
        clean_env.setenv("KNOWLEDGE_EMBEDDER", "letters")
        made = embedder_from_env()
        assert isinstance(made, Letters) and made.model == "letters-v1"
        clean_env.setenv("KNOWLEDGE_EMBED_MODEL", "letters-v2")  # the model setting is passed to it
        assert embedder_from_env().id == "letters:letters-v2"
    finally:
        EMBEDDERS.pop("letters", None)


def test_your_own_embedder_is_chosen_by_import_path(clean_env):
    clean_env.setenv("KNOWLEDGE_EMBEDDER", "tests.test_embedders:Letters")
    assert isinstance(embedder_from_env(), Letters)


def test_a_wrong_choice_says_what_exists(clean_env):
    clean_env.setenv("KNOWLEDGE_EMBEDDER", "nope")
    with pytest.raises(RuntimeError, match=r"unknown KNOWLEDGE_EMBEDDER 'nope'; known: .*litellm.*sentence-transformers"):
        embedder_from_env()
    with pytest.raises(TypeError, match="needs an Embedder subclass"):
        embedder("bad")(dict)
    clean_env.setenv("KNOWLEDGE_EMBEDDER", "tests.test_embedders:not_an_embedder")
    with pytest.raises(TypeError, match="not an Embedder"):
        embedder_from_env()


def not_an_embedder(model: str | None) -> object:
    return object()


def test_unset_picks_the_best_available(clean_env):
    assert isinstance(embedder_from_env(), FakeEmbedder)  # nothing configured: matches words, with a warning
    clean_env.setattr(emb, "_sentence_transformers_installed", lambda: True)

    class FakeST(Embedder):
        dim = 3

        def __init__(self, model: str | None = None):
            self.model = model

        async def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0] for _ in texts]

    clean_env.setitem(EMBEDDERS, "sentence-transformers", lambda model: FakeST(model))
    assert isinstance(embedder_from_env(), FakeST)  # installed: a local model
    clean_env.setattr(emb, "_chonkie_installed", lambda: True)
    clean_env.setitem(EMBEDDERS, "chonkie", lambda model: FakeEmbedder(dim=7))
    assert embedder_from_env().dim == 7  # chonkie's small model2vec model comes first (no PyTorch)
    clean_env.setenv("OPENAI_API_KEY", "sk-test")
    assert embedder_from_env().id == "litellm:openai/text-embedding-3-small"
    clean_env.setenv("KNOWLEDGE_EMBED_MODEL", "voyage/voyage-3")
    assert (e := embedder_from_env()).id == "litellm:voyage/voyage-3" and e.dim == 1024
    clean_env.setenv("SILICONFLOW_API_KEY", "sf-test")
    clean_env.delenv("KNOWLEDGE_EMBED_MODEL")
    assert embedder_from_env().id == "siliconflow:Qwen/Qwen3-Embedding-0.6B"


async def test_litellm_embedder_calls_the_named_model_and_learns_its_size(clean_env):
    calls: list[dict[str, Any]] = []

    async def fake(**kw: Any) -> dict[str, Any]:
        calls.append(kw)
        return {"data": [{"embedding": [0.1, 0.2, 0.3]} for _ in kw["input"]]}

    import litellm

    clean_env.setattr(litellm, "aembedding", fake)
    e = LiteLLMEmbedder("ollama/nomic-embed-text", api_base="http://127.0.0.1:11434", batch=2)
    assert e.dim == 0  # unknown model: learned on the first call
    assert len(await e.embed(["a", "b", "c"])) == 3 and e.dim == 3
    assert [len(c["input"]) for c in calls] == [2, 1] and calls[0]["model"] == "ollama/nomic-embed-text"
    assert calls[0]["api_base"] == "http://127.0.0.1:11434" and "api_key" not in calls[0]


async def test_sentence_transformers_runs_the_model_locally(clean_env):
    seen: dict[str, Any] = {}

    class Model:
        def __init__(self, name: str, device: str | None = None):
            seen["name"] = name

        def get_sentence_embedding_dimension(self) -> int:
            return 2

        def encode(self, texts: list[str], batch_size: int, normalize_embeddings: bool) -> list[list[float]]:
            seen["normalized"] = normalize_embeddings
            return [[1.0, 0.0] for _ in texts]

    clean_env.setitem(sys.modules, "sentence_transformers", types.SimpleNamespace(SentenceTransformer=Model))
    clean_env.setenv("KNOWLEDGE_EMBEDDER", "sentence-transformers")
    e = embedder_from_env()
    assert (e.id, e.dim) == ("sentence-transformers:sentence-transformers/all-MiniLM-L6-v2", 2)
    assert await e.embed(["hi"]) == [[1.0, 0.0]] and seen["normalized"] is True


def test_each_embedder_has_its_own_index_table():
    """Vectors from different models can't be compared: a new embedder never mixes with an existing index."""
    assert index_table(LiteLLMEmbedder("openai/text-embedding-3-small")) != index_table(LiteLLMEmbedder("voyage/voyage-3"))
    assert index_table(Letters()) != index_table(FakeEmbedder())
    sf = emb.SiliconFlowEmbedder(api_key="k")
    assert index_table(sf) == "chunks"  # the table indexes made before this change already use


async def test_a_knowledge_base_searches_with_your_embedder():
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.models import Document
    from keelchat.knowledge.service import KnowledgeService, make_indexer
    from keelchat.knowledge.vectorstore import InMemoryIndex

    e = Letters()
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), e, make_indexer("simple", e))
    await svc.add(Document.create("zz", "zzz zzz zzz", ""))
    await svc.add(Document.create("aa", "aaa aaa aaa", ""))
    assert (await svc.search("zzzz", k=1))[0].title == "zz"


@pytest.mark.skipif(not os.getenv("OPENAI_API_KEY"), reason="needs OPENAI_API_KEY")
async def test_a_real_embedding_model_puts_similar_meanings_together():
    """OpenAI's embedding model through litellm: a paraphrase is nearer than an unrelated sentence."""
    import math

    e = LiteLLMEmbedder("openai/text-embedding-3-small")
    q, para, other = await e.embed(["When do you open?", "Our opening hours start at 7am.", "Gluten-free bread is baked on Fridays."])

    def cos(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))

    assert e.dim == 1536 and cos(q, para) > cos(q, other)


# --- chunkers, indexing methods and vector stores are pluggable the same way -------------------------------------


async def test_your_own_chunker_indexing_method_and_vector_store_are_used(monkeypatch):
    """A knowledge base built entirely from parts a user wrote: each registered by name and picked by its setting."""
    from keelchat import Chunk, Chunker, Document, Hit, IndexContext, Indexer, VectorIndex, VectorStoreConfig, chunker, indexer, vector_store
    from keelchat.knowledge.chunker import chunkers
    from keelchat.knowledge.service import service_from_env
    from keelchat.knowledge.strategies import indexers
    from keelchat.knowledge.vectorstore import vector_stores

    @chunker("lines")
    class Lines(Chunker):
        def __init__(self, embedder: Embedder):
            pass

        async def chunk(self, text: str) -> list[str]:
            return [line for line in text.splitlines() if line.strip()]

    @indexer("shouty")
    def shouty(ctx: IndexContext) -> Indexer:
        class Shouty(Indexer):
            name = "shouty"

            async def chunks(self, doc: Document) -> list[Chunk]:
                texts = await ctx.chunker.chunk(doc.text)
                return [Chunk(id=f"{doc.id}:{i}", doc_id=doc.id, title=doc.title, text=t.upper(), strategy=self.name) for i, t in enumerate(texts)]

        return Shouty()

    @vector_store("listy")
    class Listy(VectorIndex):
        def __init__(self, cfg: VectorStoreConfig):
            self.cfg, self.rows = cfg, []

        async def upsert(self, chunks: list[Chunk], vectors: list[list[float]]) -> None:
            self.rows += list(zip(chunks, vectors, strict=True))

        async def search(self, vector: list[float], k: int = 5, max_level: int | None = None, dataset: str = "") -> list[Hit]:
            from keelchat.knowledge.vectorstore import cosine

            scored = sorted(((cosine(vector, v), c) for c, v in self.rows if c.dataset == dataset), key=lambda x: -x[0])
            return [Hit(chunk=c, score=s) for s, c in scored[:k]]

        async def delete_doc(self, doc_id: str) -> None:
            self.rows = [(c, v) for c, v in self.rows if c.doc_id != doc_id]

        async def count(self, dataset: str | None = None) -> int:
            return len(self.rows)

    try:
        for k, v in {"KNOWLEDGE_EMBEDDER": "hash", "KNOWLEDGE_CHUNKER": "lines", "KNOWLEDGE_STRATEGY": "shouty", "KNOWLEDGE_VECTOR_STORE": "listy"}.items():
            monkeypatch.setenv(k, v)
        for k in ("KNOWLEDGE_S3_BUCKET", "KNOWLEDGE_INDEX_URI"):  # documents stay in memory, never in a real bucket
            monkeypatch.delenv(k, raising=False)
        svc = service_from_env()
        assert type(svc.docs).__name__ == "InMemoryDocumentStore"
        assert isinstance(svc.index, Listy) and isinstance(svc.chunker, Lines) and svc.strategy == "shouty"
        await svc.add(Document.create("faq", "We open at 7am.\nParking is free behind the shop.", ""))
        hits = await svc.search("parking", k=1)
        assert [h.text for h in hits] == ["PARKING IS FREE BEHIND THE SHOP."]
        assert svc.index.cfg.table.startswith("chunks_") and svc.index.cfg.dim == 512
    finally:
        chunkers.factories.pop("lines", None)
        indexers.factories.pop("shouty", None)
        vector_stores.factories.pop("listy", None)


async def test_a_knowledge_base_can_name_a_registered_method(tmp_path, monkeypatch):
    """`strategy:` on a knowledge base takes a registered method's name; an unknown one is refused when indexed."""
    from keelchat import Chunk, Document, IndexContext, Indexer, indexer
    from keelchat.datasets.models import Dataset, DatasetIn, DocumentSource, UnstructuredDataset
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.service import KnowledgeService, make_indexer
    from keelchat.knowledge.strategies import indexers
    from keelchat.knowledge.vectorstore import InMemoryIndex

    @indexer("whole")
    def whole(ctx: IndexContext) -> Indexer:
        class Whole(Indexer):
            name = "whole"

            async def chunks(self, doc: Document) -> list[Chunk]:
                return [Chunk(id=f"{doc.id}:0", doc_id=doc.id, title=doc.title, text=doc.text, strategy="whole")]

        return Whole()

    monkeypatch.setenv("KEELCHAT_DATA_ROOT", str(tmp_path))
    (tmp_path / "a.md").write_text("Opening hours are 7am to 6pm.")
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()), auto=True)

    def ds(strategy: str) -> Dataset:
        return Dataset.create("t", "kb", DatasetIn(spec=UnstructuredDataset(documents=[DocumentSource(uri=str(tmp_path / "a.md"))], strategy=strategy)))

    try:
        await svc.index_dataset(ds("whole"), register=True)
        assert svc.method("t/kb")[0] == "whole"
        with pytest.raises(ValueError, match="unknown strategy 'nope'; known: .*whole"):
            await svc.index_dataset(ds("nope"), register=True)
    finally:
        indexers.factories.pop("whole", None)


async def test_the_sentence_window_chunker_needs_no_embeddings():
    from keelchat.knowledge.chunker import SentenceWindowChunker

    text = "First point here. Second point here.\n\nNew paragraph starts. It goes on."
    assert await SentenceWindowChunker(max_words=200, min_words=2).chunk(text) == ["First point here. Second point here.", "New paragraph starts. It goes on."]
    assert await SentenceWindowChunker(max_words=4, min_words=1).chunk("One two three. Four five six.") == ["One two three.", "Four five six."]


async def test_the_registry_lists_the_knowledge_parts(monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from keelchat.api.app import create_app
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.service import KnowledgeService, make_indexer
    from keelchat.knowledge.vectorstore import InMemoryIndex
    from keelchat.runtime.llm import ModelConfig
    from keelchat.runtime.runtime import Runtime
    from keelchat.state.store import InMemoryTenantToolStore
    from tests.helpers import RecordingLLM

    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    app = create_app(Runtime(RecordingLLM(), {}, models=ModelConfig.from_env()), tool_store=InMemoryTenantToolStore(), knowledge=svc)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        k = (await c.get("/v1/registry")).json()["knowledge"]
    assert {"litellm", "sentence-transformers", "hash"} <= set(k["embedders"]) and {"semantic", "sentences"} <= set(k["chunkers"])
    assert {"simple", "contextual", "raptor", "full_context"} <= set(k["strategies"]) and {"memory", "lancedb"} <= set(k["vector_stores"])
    assert k["using"] == {"embedder": "hash:512", "chunker": "SemanticChunker", "strategy": "simple"}


async def test_a_bad_method_leaves_the_knowledge_base_as_it_was(tmp_path, monkeypatch):
    """Re-registering with an unknown method fails before anything is removed, and the API refuses it with its path."""
    from httpx import ASGITransport, AsyncClient

    from keelchat.api.app import create_app
    from keelchat.datasets.models import Dataset, DatasetIn, DocumentSource, UnstructuredDataset
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.service import KnowledgeService, make_indexer
    from keelchat.knowledge.vectorstore import InMemoryIndex
    from keelchat.runtime.llm import ModelConfig
    from keelchat.runtime.runtime import Runtime
    from keelchat.state.store import InMemoryTenantToolStore
    from tests.helpers import RecordingLLM

    monkeypatch.setenv("KEELCHAT_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("KNOWLEDGE_URL", raising=False)
    (tmp_path / "a.md").write_text("Opening hours are 7am to 6pm.")
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))

    def ds(strategy: str) -> Dataset:
        return Dataset.create("t", "kb", DatasetIn(spec=UnstructuredDataset(documents=[DocumentSource(uri=str(tmp_path / "a.md"))], strategy=strategy)))

    await svc.index_dataset(ds("simple"), register=True)
    before = (await svc.docs.ids("t/kb"), await svc.index.count("t/kb"))
    with pytest.raises(ValueError, match="unknown strategy 'raptr'"):
        await svc.index_dataset(ds("raptr"), register=True)
    assert (await svc.docs.ids("t/kb"), await svc.index.count("t/kb")) == before and before[1] > 0

    app = create_app(Runtime(RecordingLLM(), {}, models=ModelConfig.from_env()), tool_store=InMemoryTenantToolStore(), knowledge=svc)
    body = {"spec": {"kind": "unstructured", "documents": [{"uri": str(tmp_path / "a.md")}], "strategy": "raptr"}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.put("/v1/tenants/default/datasets/kb", json=body)
    assert r.status_code == 422 and r.json()["detail"][0]["path"] == "spec.strategy" and "raptr" in r.json()["detail"][0]["message"]


async def test_a_method_is_known_by_its_registered_name_and_can_read_whole_documents():
    from keelchat import Chunk, Document, Indexer, indexer
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.plugins import plugin_name
    from keelchat.knowledge.service import KnowledgeService, make_indexer
    from keelchat.knowledge.strategies import indexers
    from keelchat.knowledge.vectorstore import InMemoryIndex

    class Whole(Indexer):
        name = "something-else"
        retrieval = "whole_documents"

        async def chunks(self, doc: Document) -> list[Chunk]:
            return []

    indexer("whole")(lambda ctx: Whole())
    try:
        made = make_indexer("whole", FakeEmbedder())
        assert made.name == "whole" and plugin_name(made) == "whole"
        svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), made)
        assert svc.strategy == "whole"
        await svc.add(Document.create("faq", "We open at 7am.", ""))
        assert [p.text for p in await svc.search("when do you open")] == ["We open at 7am."]  # whole documents, no vectors
        assert await svc.reindex_if_empty() == 0
    finally:
        indexers.factories.pop("whole", None)


def test_building_parts_reads_no_settings(monkeypatch):
    """Only service_from_env reads the environment: a broken KNOWLEDGE_CHUNKER does not affect direct construction."""
    from keelchat.knowledge.chunker import SemanticChunker
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.service import KnowledgeService, make_indexer
    from keelchat.knowledge.vectorstore import InMemoryIndex

    monkeypatch.setenv("KNOWLEDGE_CHUNKER", "not_installed.module:Nope")
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    assert isinstance(svc.chunker, SemanticChunker)


def test_your_part_keeps_a_built_in_name(monkeypatch):
    """A store registered as "pgvector" before the optional built-ins load is the one used, not the built-in."""
    import sys

    from keelchat import VectorIndex, VectorStoreConfig, vector_store
    from keelchat.knowledge import plugins
    from keelchat.knowledge.vectorstore import InMemoryIndex, vector_stores

    vector_stores.names()  # load the built-ins once, so there is one to set aside
    original = vector_stores.factories["pgvector"]
    vector_stores.factories.pop("pgvector")
    loaded = plugins._builtins_loaded
    try:
        plugins._builtins_loaded = False  # as in a fresh process, where the built-ins load on first lookup
        monkeypatch.delitem(sys.modules, "keelchat.knowledge.stores")  # so loading them runs their registrations again

        @vector_store("pgvector")
        class Mine(InMemoryIndex):
            def __init__(self, cfg: VectorStoreConfig):
                super().__init__()

        made = vector_stores.make("pgvector", VectorStoreConfig(None, "chunks_x", 4))
        assert isinstance(made, Mine) and isinstance(made, VectorIndex)
        assert "keelchat.knowledge.stores" in sys.modules  # the built-ins did load, and left "pgvector" alone
    finally:
        vector_stores.factories["pgvector"] = original
        plugins._builtins_loaded = loaded

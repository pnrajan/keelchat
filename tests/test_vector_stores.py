"""Vector stores pass one shared check (keelchat.testing.check_vector_store); chonkie chunks and embeds."""

from __future__ import annotations

import os
import uuid

import pytest

from keelchat.knowledge.vectorstore import InMemoryIndex, LanceDBIndex, VectorStoreConfig, vector_stores
from keelchat.testing import check_vector_store


async def test_memory_store_passes_the_check():
    await check_vector_store(InMemoryIndex())


async def test_lancedb_store_passes_the_check(tmp_path):
    await check_vector_store(LanceDBIndex(str(tmp_path / "lance"), table="chunks_test", dim=4))


async def test_qdrant_store_passes_the_check():
    await check_vector_store(vector_stores.make("qdrant", VectorStoreConfig(None, "chunks_test", 4)))  # in memory


async def test_chroma_store_passes_the_check():
    await check_vector_store(vector_stores.make("chroma", VectorStoreConfig(None, f"chunks_{uuid.uuid4().hex[:8]}", 4)))


@pytest.mark.skipif(not os.getenv("KEELCHAT_TEST_PGVECTOR_URL"), reason="needs a Postgres with pgvector (KEELCHAT_TEST_PGVECTOR_URL)")
async def test_pgvector_store_passes_the_check(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_PGVECTOR_URL", os.environ["KEELCHAT_TEST_PGVECTOR_URL"])
    table = f"chunks_t{uuid.uuid4().hex[:8]}"
    store = vector_stores.make("pgvector", VectorStoreConfig(None, table, 4))
    try:
        await check_vector_store(store)
    finally:
        pool = await store._db()  # type: ignore[attr-defined]
        await pool.execute(f"DROP TABLE IF EXISTS {table}")
        await pool.close()


async def test_the_check_catches_a_store_that_ignores_datasets():
    """The check is strict enough to matter: a store that searches across datasets fails it, by name."""
    from keelchat.knowledge.models import Hit
    from keelchat.knowledge.vectorstore import cosine

    class Leaky(InMemoryIndex):
        async def search(self, vector, k=5, max_level=None, dataset=""):  # type: ignore[no-untyped-def]
            hits = [Hit(chunk=c, score=cosine(vector, v)) for c, v in self._rows.values()]
            return sorted(hits, key=lambda h: -h.score)[:k]

    with pytest.raises(AssertionError, match="dataset"):
        await check_vector_store(Leaky())


def test_every_store_is_registered_and_a_missing_setting_is_explained(monkeypatch):
    assert {"memory", "lancedb", "qdrant", "chroma", "pgvector"} <= set(vector_stores.names())
    monkeypatch.delenv("KNOWLEDGE_PGVECTOR_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="pgvector needs KNOWLEDGE_PGVECTOR_URL or DATABASE_URL"):
        vector_stores.make("pgvector", VectorStoreConfig(None, "chunks_x", 4))


# --- chonkie -------------------------------------------------------------------------------------------------------

TEXT = (
    "Our bakery opens at 7am every day except Monday. Weekend hours are the same as weekdays. "
    "We close at 6pm.\n\nParking is free behind the shop. There are ten spaces, and bikes can be locked at the front. "
    "\n\nCustom cakes need three days' notice. A deposit of twenty dollars holds the order. Cakes can be picked up after noon."
)


LONG = "\n\n".join(
    " ".join(f"{topic} detail {i}: {fact}." for i in range(8))
    for topic, fact in (
        ("Opening hours", "the bakery opens at seven in the morning and closes at six in the evening"),
        ("Parking", "cars can park for free in the lot behind the shop"),
        ("Custom cakes", "a birthday cake needs three days notice and a deposit"),
    )
)


async def test_chonkie_splits_a_document_and_keeps_every_sentence():
    from keelchat.knowledge.chonkie_parts import ChonkieChunker

    chunks = await ChonkieChunker(chunk_size=120).chunk(LONG)
    assert len(chunks) >= 2 and all(c.strip() for c in chunks)
    joined = " ".join(chunks)
    assert all(f"{t} detail {i}" in joined for t in ("Opening hours", "Parking", "Custom cakes") for i in range(8))  # nothing lost
    assert await ChonkieChunker().chunk("   ") == []
    for kind in ("recursive", "sentence", "token"):
        assert await ChonkieChunker(kind=kind, chunk_size=40).chunk(TEXT), kind
    with pytest.raises(ValueError, match="unknown chonkie chunker"):
        ChonkieChunker(kind="nope")  # type: ignore[arg-type]


async def test_chonkie_embeds_locally_and_finds_the_right_passage():
    from keelchat.knowledge.chonkie_parts import ChonkieChunker, ChonkieEmbedder
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.models import Document
    from keelchat.knowledge.service import KnowledgeService, make_indexer

    e = ChonkieEmbedder()
    assert e.id == "chonkie:minishlab/potion-base-8M" and e.dim == 0  # nothing loaded until it is used
    chunker = ChonkieChunker(chunk_size=60, threshold=0.6)
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), e, make_indexer("simple", e, chunker=chunker), chunker=chunker)
    await svc.add(Document.create("faq", TEXT, ""))
    assert "Parking" in (await svc.search("where can I leave my car", k=1))[0].text
    assert e.dim > 0


def test_with_chonkie_installed_it_is_the_default_chunker_and_local_embedder(monkeypatch):
    from keelchat.knowledge import embedder as emb
    from keelchat.knowledge.chonkie_parts import ChonkieChunker, ChonkieEmbedder
    from keelchat.knowledge.chunker import chunker_from_env

    for v in ("KNOWLEDGE_EMBEDDER", "KNOWLEDGE_EMBED_MODEL", "SILICONFLOW_API_KEY", "OPENAI_API_KEY", "KNOWLEDGE_CHUNKER"):
        monkeypatch.delenv(v, raising=False)
    e = emb.embedder_from_env()
    assert isinstance(e, ChonkieEmbedder)  # no API embedding model: a local one, not word matching
    c = chunker_from_env(e)
    assert isinstance(c, ChonkieChunker) and c.kind == "semantic" and c.chunk_size == 512
    monkeypatch.setenv("KNOWLEDGE_CHONKIE", "recursive")
    monkeypatch.setenv("KNOWLEDGE_CHUNK_SIZE", "300")
    c = chunker_from_env(e)
    assert (c.kind, c.chunk_size) == ("recursive", 300)


async def test_a_new_vector_store_is_filled_from_documents_already_stored(tmp_path, monkeypatch):
    """Documents outlive the vectors when the store or embedder changes (each embedder has its own table): a knowledge
    base whose documents are stored but whose vectors are missing is embedded again, not skipped as unchanged."""
    from keelchat.datasets.models import Dataset, DatasetIn, DocumentSource, UnstructuredDataset
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.embedder import FakeEmbedder
    from keelchat.knowledge.service import KnowledgeService, make_indexer

    monkeypatch.setenv("KEELCHAT_DATA_ROOT", str(tmp_path))
    (tmp_path / "faq.md").write_text("Parking is free behind the shop.")
    ds = Dataset.create("t", "kb", DatasetIn(spec=UnstructuredDataset(documents=[DocumentSource(uri=str(tmp_path / "faq.md"))], strategy="simple")))
    docs = InMemoryDocumentStore()  # kept, like documents in S3
    first = KnowledgeService(docs, InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    assert await first.index_dataset(ds, register=True) > 0
    moved = KnowledgeService(docs, InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))  # a new, empty store
    assert await moved.index_dataset(ds, register=True) > 0
    assert (await moved.search("parking", dataset=ds))[0].text == "Parking is free behind the shop."

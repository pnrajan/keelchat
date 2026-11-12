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
        await check_vector_store(store, crowd=20_000)  # enough rows that Postgres uses the HNSW index
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


@pytest.mark.skipif(not os.getenv("KEELCHAT_TEST_PGVECTOR_URL"), reason="needs a Postgres with pgvector (KEELCHAT_TEST_PGVECTOR_URL)")
async def test_pgvector_finds_a_small_dataset_when_postgres_uses_the_vector_index(monkeypatch):
    """When the planner reads the HNSW index and filters afterwards, a dataset whose vectors are not among the nearest
    few dozen overall must still be found. Forced here by turning other plans off for this database's new sessions."""
    import asyncpg

    from keelchat.knowledge.models import Chunk
    from keelchat.testing.vector_store import _unit

    url = os.environ["KEELCHAT_TEST_PGVECTOR_URL"]
    table = f"chunks_t{uuid.uuid4().hex[:8]}"
    admin = await asyncpg.connect(url)
    db = await admin.fetchval("SELECT current_database()")
    try:
        await admin.execute(f'ALTER DATABASE "{db}" SET enable_seqscan = off')
        await admin.execute(f'ALTER DATABASE "{db}" SET enable_bitmapscan = off')
        await admin.execute(f'ALTER DATABASE "{db}" SET enable_sort = off')
        monkeypatch.setenv("KNOWLEDGE_PGVECTOR_URL", url)
        store = vector_stores.make("pgvector", VectorStoreConfig(None, table, 4))
        big = [Chunk(id=f"b{i}", doc_id=f"bd{i // 50}", title="t", text="x", dataset="t/big") for i in range(3000)]
        await store.upsert(big, [_unit(1, 0.001 * (i % 50), 0.001 * (i // 50), 0) for i in range(3000)])
        await store.upsert([Chunk(id="s0", doc_id="sd", title="t", text="y", dataset="t/small")], [_unit(0.5, 0, 0, 0.866)])
        pool = await store._db()  # type: ignore[attr-defined]
        plan = "\n".join(r[0] for r in await pool.fetch(f"EXPLAIN SELECT id FROM {table} WHERE dataset = 't/small' ORDER BY embedding <=> '[1,0,0,0]' LIMIT 3"))
        assert "hnsw" in plan, plan  # the plan that loses results without the fix
        assert [h.chunk.id for h in await store.search(_unit(1, 0, 0, 0), k=3, dataset="t/small")] == ["s0"]
    finally:
        await admin.execute(f'ALTER DATABASE "{db}" RESET ALL')
        await admin.execute(f"DROP TABLE IF EXISTS {table}")
        await admin.close()


@pytest.mark.skipif(not os.getenv("KEELCHAT_TEST_PGVECTOR_URL"), reason="needs a Postgres with pgvector (KEELCHAT_TEST_PGVECTOR_URL)")
async def test_pgvector_takes_embeddings_too_large_for_its_index(monkeypatch):
    """pgvector's HNSW index stops at 2000 dimensions (text-embedding-3-large has 3072): the first write works, with exact search."""
    from keelchat.knowledge.models import Chunk

    monkeypatch.setenv("KNOWLEDGE_PGVECTOR_URL", os.environ["KEELCHAT_TEST_PGVECTOR_URL"])
    table = f"chunks_t{uuid.uuid4().hex[:8]}"
    store = vector_stores.make("pgvector", VectorStoreConfig(None, table, 3072))
    try:
        v = [1.0] + [0.0] * 3071
        await store.upsert([Chunk(id="a", doc_id="d", title="t", text="x", dataset="t/a")], [v])
        assert [h.chunk.id for h in await store.search(v, k=1, dataset="t/a")] == ["a"]
        assert await store.search([0.0] * 3072, k=1, dataset="t/a") == []  # no direction, no match
    finally:
        pool = await store._db()  # type: ignore[attr-defined]
        await pool.execute(f"DROP TABLE IF EXISTS {table}")
        await pool.close()


def test_qdrant_and_chroma_refuse_an_s3_address_instead_of_a_folder_named_after_it(monkeypatch, tmp_path):
    for var in ("QDRANT_URL", "CHROMA_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    for kind, var in (("qdrant", "QDRANT_URL"), ("chroma", "CHROMA_URL")):
        with pytest.raises(ValueError, match=f"set {var} to a {kind} server, or KNOWLEDGE_INDEX_URI to a local folder"):
            vector_stores.make(kind, VectorStoreConfig("s3://bucket/knowledge/index", "chunks_x", 4))
    assert not any(tmp_path.iterdir())  # nothing written locally


def _kb(tmp_path, monkeypatch, strategy="simple", files=None):  # type: ignore[no-untyped-def]
    from keelchat.datasets.models import Dataset, DatasetIn, DocumentSource, UnstructuredDataset

    monkeypatch.setenv("KEELCHAT_DATA_ROOT", str(tmp_path))
    files = files or {"hours.md": "We open at 7am.", "parking.md": "Parking is free behind the shop.", "cakes.md": "Custom cakes need a deposit."}
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    docs = [DocumentSource(uri=str(tmp_path / n)) for n in files]
    return Dataset.create("t", "kb", DatasetIn(spec=UnstructuredDataset(documents=docs, strategy=strategy)))


async def test_a_refill_that_failed_part_way_is_finished_on_the_next_run(tmp_path, monkeypatch):
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.embedder import FakeEmbedder
    from keelchat.knowledge.service import KnowledgeService, make_indexer

    ds = _kb(tmp_path, monkeypatch)
    docs = InMemoryDocumentStore()
    await KnowledgeService(docs, InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder())).index_dataset(ds, register=True)

    class Flaky(FakeEmbedder):  # embeds the first document into the new store, then fails
        async def embed(self, texts):  # type: ignore[no-untyped-def]
            if self.calls >= 2:
                raise RuntimeError("embedding service down")
            return await super().embed(texts)

    new = InMemoryIndex()
    flaky = Flaky()
    with pytest.raises(RuntimeError):
        await KnowledgeService(docs, new, flaky, make_indexer("simple", flaky)).index_dataset(ds, register=True)
    svc = KnowledgeService(docs, new, FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    await svc.index_dataset(ds, register=True)
    assert await new.doc_ids("t/kb") == set(await docs.ids("t/kb")) and len(await docs.ids("t/kb")) == 3
    assert "deposit" in (await svc.search("custom cakes deposit", k=1, dataset=ds))[0].text


async def test_a_changed_method_is_applied_after_a_restart(tmp_path, monkeypatch):
    """The method is part of what was indexed: another process (or a restart) sees the change and re-indexes."""
    from keelchat.knowledge.docstore import InMemoryDocumentStore
    from keelchat.knowledge.embedder import FakeEmbedder
    from keelchat.knowledge.service import KnowledgeService, make_indexer

    docs, index = InMemoryDocumentStore(), InMemoryIndex()
    await KnowledgeService(docs, index, FakeEmbedder(), make_indexer("simple", FakeEmbedder())).index_dataset(_kb(tmp_path, monkeypatch), register=True)
    assert {c.strategy for c, _ in index._rows.values()} == {"simple"}
    restarted = KnowledgeService(docs, index, FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    n = await restarted.index_dataset(_kb(tmp_path, monkeypatch, strategy="full_context"), register=True)
    assert restarted.method("t/kb")[0] == "full_context" and n == 0
    assert await index.count("t/kb") == 0  # the old method's vectors are gone
    assert [p.text for p in await restarted.search("parking", dataset=_kb(tmp_path, monkeypatch, strategy="full_context"))][0].startswith("Parking")

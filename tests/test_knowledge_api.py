from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from yachat.knowledge.api import create_app
from yachat.knowledge.docstore import InMemoryDocumentStore
from yachat.knowledge.embedder import FakeEmbedder
from yachat.knowledge.models import Document
from yachat.knowledge.service import KnowledgeService, make_indexer
from yachat.knowledge.vectorstore import InMemoryIndex


@pytest.fixture
async def client():
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    async with AsyncClient(transport=ASGITransport(app=create_app(svc)), base_url="http://t") as c:
        yield c


async def test_index_search_delete(client):
    r = await client.post(
        "/documents",
        json={
            "documents": [
                {"title": "Benefits", "text": "We offer health, dental and a 401k match for full-time staff.", "source": "hr/benefits.md"},
                {"title": "Parking", "text": "Garage parking is free for employees after 6pm."},
            ]
        },
    )
    assert r.status_code == 201 and [d["title"] for d in r.json()] == ["Benefits", "Parking"] and all(d["chunks"] >= 1 for d in r.json())
    h = (await client.get("/health")).json()
    assert h["status"] == "ok" and h["strategy"] == "simple" and h["documents"] == 2 and h["chunks"] >= 2
    r = await client.post("/search", json={"query": "dental benefits"})
    hits = r.json()["results"]
    assert r.status_code == 200 and hits[0]["title"] == "Benefits" and {"title", "text", "source", "score"} <= hits[0].keys()
    doc_id = (await client.get("/documents")).json()[0]["id"]
    assert (await client.delete(f"/documents/{doc_id}")).json() == {"deleted": doc_id}
    assert (await client.delete(f"/documents/{doc_id}")).status_code == 404
    assert (await client.get("/health")).json()["documents"] == 1


async def test_rejects_bad_input(client):
    assert (await client.post("/documents", json={"documents": []})).status_code == 422
    assert (await client.post("/documents", json={"documents": [{"title": "x", "text": ""}]})).status_code == 422
    assert (await client.post("/search", json={"query": "", "extra": 1})).status_code == 422
    assert (await client.post("/search", json={"query": "q", "k": 0})).status_code == 422


async def test_api_key_gates_documents_and_search(monkeypatch):
    monkeypatch.setenv("YACHAT_API_KEY", "secret")
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    async with AsyncClient(transport=ASGITransport(app=create_app(svc)), base_url="http://t") as c:
        assert (await c.get("/health")).status_code == 200
        assert (await c.post("/search", json={"query": "x"})).status_code == 401
        ok = await c.post("/documents", json={"documents": [{"title": "t", "text": "hello world"}]}, headers={"authorization": "Bearer secret"})
        assert ok.status_code == 201


async def test_restart_rebuilds_an_empty_index_from_stored_documents():
    docs = InMemoryDocumentStore()
    first = KnowledgeService(docs, InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    await first.add(Document.create("Parking", "Garage parking is free for employees on level two.", "hr/parking"))
    restarted = KnowledgeService(docs, InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))  # same docs, fresh index
    assert await restarted.reindex_if_empty() >= 1 and (await restarted.search("garage parking"))[0].title == "Parking"
    assert await restarted.reindex_if_empty() == 0  # already populated: nothing to do


@pytest.fixture
def docs_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    (root / "returns.md").write_text("Returns are accepted within 30 days with a receipt.")
    (root / "shipping.txt").write_text("Standard shipping takes five business days.")
    monkeypatch.setenv("YACHAT_DATA_ROOT", str(root))
    return root


def _dataset(tenant: str, *files: str, root=None) -> dict:
    from yachat.datasets.models import Dataset, DatasetIn, DocumentSource, UnstructuredDataset

    spec = UnstructuredDataset(documents=[DocumentSource(uri=str(root / f)) for f in files])
    return Dataset.create(tenant, "policies", DatasetIn(spec=spec)).model_dump(mode="json")


async def test_datasets_are_isolated_from_each_other_and_the_shared_corpus(client, docs_root):
    await client.post("/documents", json={"documents": [{"title": "Corpus", "text": "Returns policy for the shared corpus: 14 days."}]})
    acme = _dataset("acme", "returns.md", "shipping.txt", root=docs_root)
    r = await client.post("/datasets", json=acme)
    assert r.status_code == 200 and r.json()["dataset"] == "acme/policies" and r.json()["chunks"] >= 2
    assert (await client.post("/datasets", json=acme)).json()["chunks"] == 0  # same version: not re-indexed
    hits = (await client.post("/search", json={"query": "returns days", "dataset": acme})).json()["results"]
    assert hits and all(h["source"].startswith(str(docs_root)) for h in hits) and hits[0]["title"] == "returns.md"
    shared = (await client.post("/search", json={"query": "returns days"})).json()["results"]
    assert [h["title"] for h in shared] == ["Corpus"]  # no dataset: the shared corpus only
    other = _dataset("other", "shipping.txt", root=docs_root)
    hits = (await client.post("/search", json={"query": "returns days", "dataset": other})).json()["results"]
    assert {h["title"] for h in hits} == {"shipping.txt"}  # indexed on first use; never sees acme's documents


async def test_new_dataset_version_replaces_its_documents(client, docs_root):
    await client.post("/datasets", json=_dataset("acme", "returns.md", "shipping.txt", root=docs_root))
    await client.post("/datasets", json=_dataset("acme", "shipping.txt", root=docs_root))
    hits = (await client.post("/search", json={"query": "returns receipt", "k": 10, "dataset": _dataset("acme", "shipping.txt", root=docs_root)})).json()
    assert {h["title"] for h in hits["results"]} == {"shipping.txt"}
    assert (await client.delete("/datasets/acme/policies")).json()["documents"] == 1
    assert (await client.get("/health")).json()["documents"] == 0


async def test_unreadable_dataset_sources_are_rejected(client, docs_root, tmp_path):
    (tmp_path / "secret.txt").write_text("secret")
    bad = _dataset("acme", "returns.md", root=docs_root)
    bad["spec"]["documents"][0]["uri"] = str(tmp_path / "secret.txt")  # outside the data root
    assert (await client.post("/datasets", json=bad)).status_code == 422
    r = await client.post("/search", json={"query": "secret", "dataset": bad})
    assert r.status_code == 422 and "secret" not in r.text


async def test_a_drop_during_indexing_wins(docs_root):
    import asyncio

    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    from yachat.datasets.models import Dataset

    ds = Dataset.model_validate(_dataset("acme", "returns.md", "shipping.txt", root=docs_root))
    gate = asyncio.Event()
    real_add = svc.add

    async def slow_add(doc):  # indexing is mid-way when the drop arrives
        await gate.wait()
        return await real_add(doc)

    svc.add = slow_add  # type: ignore[method-assign]
    indexing = asyncio.create_task(svc.index_dataset(ds))
    await asyncio.sleep(0)
    dropping = asyncio.create_task(svc.drop_dataset(ds.ref))
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(indexing, dropping)
    assert await svc.docs.list() == [] and await svc.index.count() == 0
    assert await svc.index_dataset(ds) == 0  # the dropped version is never indexed again; a new version would be


async def test_a_restart_or_a_description_change_does_not_embed_again(docs_root):
    from yachat.datasets.models import Dataset, DatasetIn, DocumentSource, UnstructuredDataset

    class Counting(FakeEmbedder):
        calls = 0

        async def embed(self, texts):
            Counting.calls += len(texts)
            return await super().embed(texts)

    docs, index = InMemoryDocumentStore(), InMemoryIndex()
    spec = UnstructuredDataset(documents=[DocumentSource(uri=str(docs_root / "returns.md"))])
    first = KnowledgeService(docs, index, Counting(), make_indexer("simple", FakeEmbedder()))
    v1 = Dataset.create("acme", "policies", DatasetIn(spec=spec))
    assert await first.index_dataset(v1) >= 1 and Counting.calls >= 1
    before = Counting.calls
    restarted = KnowledgeService(docs, index, Counting(), make_indexer("simple", FakeEmbedder()))  # new process, same stores
    v2 = Dataset.create("acme", "policies", DatasetIn(spec=spec, description="now with a description"))
    assert await restarted.index_dataset(v2) == 0 and Counting.calls == before
    assert (await restarted.search("returns receipt", dataset=v2))[0].title == "returns.md"


async def test_a_deleted_dataset_registered_again_is_searchable(client, docs_root):
    acme = _dataset("acme", "returns.md", "shipping.txt", root=docs_root)
    await client.post("/datasets", json=acme)
    await client.delete("/datasets/acme/policies")
    assert (await client.post("/datasets", json=acme)).json()["chunks"] >= 2  # same spec, same version: indexed again
    hits = (await client.post("/search", json={"query": "returns days", "dataset": acme})).json()["results"]
    assert hits and hits[0]["title"] == "returns.md"


async def test_the_shared_corpus_and_datasets_stay_apart(client, docs_root):
    await client.post("/documents", json={"documents": [{"title": "Corpus", "text": "Returns policy for the shared corpus: 14 days."}]})
    acme = _dataset("acme", "returns.md", "shipping.txt", root=docs_root)
    await client.post("/datasets", json=acme)
    assert [d["title"] for d in (await client.get("/documents")).json()] == ["Corpus"]  # dataset documents are not listed
    svc = client._transport.app.state.service  # type: ignore[attr-defined]
    ids = sorted(await svc.docs.ids("acme/policies"))
    assert len(ids) == 2 and await svc.index.count(dataset="") >= 1
    # a dataset document removed by hand comes back on the next search, from the dataset's sources
    await svc.remove(ids[0], "acme/policies")
    hits = (await client.post("/search", json={"query": "returns days", "k": 10, "dataset": acme})).json()["results"]
    assert {h["title"] for h in hits} == {"returns.md", "shipping.txt"}
    assert (await client.get("/health")).json()["documents"] == 3


async def test_a_restart_rebuilds_the_shared_corpus_even_when_datasets_are_indexed(docs_root):
    from yachat.datasets.models import Dataset

    docs, index = InMemoryDocumentStore(), InMemoryIndex()
    svc = KnowledgeService(docs, index, FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    await svc.index_dataset(Dataset.model_validate(_dataset("acme", "returns.md", root=docs_root)))
    from yachat.knowledge.models import Document

    await docs.put(Document.create("Corpus", "Returns policy for the shared corpus: 14 days."))  # stored, not indexed (a lost index)
    assert await svc.reindex_if_empty() >= 1  # dataset chunks no longer hide an empty shared corpus

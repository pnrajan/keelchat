"""The knowledge index: one shared location in the knowledge bucket, single-commit upserts, retried commit races."""

from __future__ import annotations

from pathlib import Path

import pytest

from keelchat.knowledge.models import Chunk
from keelchat.knowledge.service import index_uri

lancedb = pytest.importorskip("lancedb")

from keelchat.knowledge.vectorstore import LanceDBIndex, _committed  # noqa: E402


@pytest.mark.parametrize(
    ("bucket", "prefix", "uri"),
    [
        ("kb", "knowledge/docs/", "s3://kb/knowledge/index"),
        ("kb", "acme/knowledge/docs", "s3://kb/acme/knowledge/index"),
        ("kb", "docs/", "s3://kb/index"),
        (None, "knowledge/docs/", None),
    ],
)
def test_every_server_finds_the_index_beside_the_documents(monkeypatch, bucket, prefix, uri):
    monkeypatch.delenv("KNOWLEDGE_INDEX_URI", raising=False)
    assert index_uri(bucket, prefix) == uri


def test_an_explicit_index_location_wins(monkeypatch):
    monkeypatch.setenv("KNOWLEDGE_INDEX_URI", "/data/lancedb")
    assert index_uri("kb", "knowledge/docs/") == "/data/lancedb"


def chunk(i: int, doc: str = "d1", text: str = "t") -> Chunk:
    return Chunk(id=f"{doc}-{i}", doc_id=doc, title="T", text=f"{text}{i}", level=0, strategy="simple")


async def test_upserting_the_same_chunks_again_replaces_them(tmp_path: Path):
    index = LanceDBIndex(str(tmp_path / "idx"), dim=4)
    v = [[1.0, 0.0, 0.0, 0.0]] * 3
    await index.upsert([chunk(i) for i in range(3)], v)
    await index.upsert([chunk(i, text="new") for i in range(3)] + [chunk(9, doc="d2")], v + [[0.0, 1.0, 0.0, 0.0]])
    assert await index.count() == 4
    hits = await index.search([1.0, 0.0, 0.0, 0.0], k=3)
    assert {h.chunk.text for h in hits} == {"new0", "new1", "new2"}
    await index.delete_doc("d1")
    assert await index.count() == 1


def test_a_lost_commit_race_is_tried_again(monkeypatch):
    monkeypatch.setattr("keelchat.knowledge.vectorstore.time.sleep", lambda s: None)
    calls = []

    def commit():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("lance error: Too many concurrent writers. Attempted 6 times")

    _committed(commit)
    assert len(calls) == 3


def test_other_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr("keelchat.knowledge.vectorstore.time.sleep", lambda s: None)
    calls = []

    def commit():
        calls.append(1)
        raise RuntimeError("lance error: schema mismatch")

    with pytest.raises(RuntimeError, match="schema"):
        _committed(commit)
    assert len(calls) == 1


async def test_servers_sharing_an_index_see_each_others_changes(tmp_path: Path):
    a = LanceDBIndex(str(tmp_path / "idx"), dim=4, consistency_s=0)
    b = LanceDBIndex(str(tmp_path / "idx"), dim=4, consistency_s=0)
    v = [1.0, 0.0, 0.0, 0.0]
    await a.upsert([chunk(1, doc="d1"), chunk(2, doc="d2")], [v, v])
    assert await b.count() == 2  # b opens the table
    await a.delete_doc("d1")  # a change after b opened it
    await a.upsert([chunk(3, doc="d3")], [v])
    assert {h.chunk.doc_id for h in await b.search(v, k=5)} == {"d2", "d3"}


async def test_s3_documents_are_kept_per_scope_and_listed_without_reading_them():
    import os
    import uuid

    from keelchat.knowledge.docstore import ArtifactDocumentStore
    from keelchat.knowledge.models import Document
    from keelchat.state.artifacts import S3ArtifactStore

    if not os.getenv("KNOWLEDGE_S3_BUCKET"):
        pytest.skip("needs KNOWLEDGE_S3_BUCKET")
    store = ArtifactDocumentStore(S3ArtifactStore(os.environ["KNOWLEDGE_S3_BUCKET"]), prefix=f"keelchat-verify/docs-{uuid.uuid4().hex[:8]}/")
    shared, mine = Document.create("Corpus", "shared text"), Document.create("FAQ", "dataset text", "faq.md", dataset="acme/faq")
    try:
        await store.put(shared)
        await store.put(mine)
        assert [d.id for d in await store.list("")] == [shared.id]  # the shared folder does not descend into datasets/
        assert await store.ids("acme/faq") == {mine.id} and await store.count() == 2
        assert (await store.get(mine.id, "acme/faq")).title == "FAQ" and await store.get(mine.id) is None
    finally:
        await store.delete(shared.id)
        await store.delete(mine.id, "acme/faq")
    assert await store.count() == 0

"""The artifact store port: every backend keeps files the same way, so the backend can be switched by configuration."""

from __future__ import annotations

import os
import uuid

import pytest

from keelchat.state.artifacts import ArtifactExists, InMemoryArtifactStore, LocalArtifactStore, S3ArtifactStore, artifact_store_from_env


@pytest.fixture(params=["memory", "local", "s3"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryArtifactStore(), ""
    if request.param == "local":
        return LocalArtifactStore(tmp_path), ""
    bucket = os.getenv("KNOWLEDGE_S3_BUCKET")
    if not bucket:
        pytest.skip("needs KNOWLEDGE_S3_BUCKET")
    return S3ArtifactStore(bucket), f"keelchat-verify/artifacts-{uuid.uuid4().hex[:8]}/"


async def test_every_backend_keeps_files_the_same_way(store):
    s, base = store
    try:
        ref = await s.put(f"{base}uploads/acme/faq.md", b"# FAQ", content_type="text/markdown")
        assert ref.endswith("uploads/acme/faq.md")
        assert await s.get(f"{base}uploads/acme/faq.md") == b"# FAQ" and await s.get(f"{base}uploads/acme/none.md") is None
        with pytest.raises(ArtifactExists):  # a file is never overwritten by accident
            await s.put(f"{base}uploads/acme/faq.md", b"other")
        await s.put(f"{base}uploads/acme/faq.md", b"# FAQ v2", replace=True)
        assert await s.get(f"{base}uploads/acme/faq.md") == b"# FAQ v2"
        await s.put(f"{base}uploads/acme/sub/deep.md", b"x")
        assert await s.keys(f"{base}uploads/acme/") == [f"{base}uploads/acme/faq.md", f"{base}uploads/acme/sub/deep.md"]
        assert await s.keys(f"{base}uploads/acme/", recursive=False) == [f"{base}uploads/acme/faq.md"]
    finally:
        await s.delete(f"{base}uploads/acme/faq.md")
        await s.delete(f"{base}uploads/acme/sub/deep.md")
        await s.delete(f"{base}uploads/acme/faq.md")  # deleting what isn't there is fine
    assert await s.keys(f"{base}uploads/") == []


@pytest.mark.parametrize("key", ["../etc/passwd", "/abs", "a/../../b", "a//b", "a/./b", "", "sp ace"])
async def test_keys_that_could_escape_the_store_are_refused(tmp_path, key):
    for s in (InMemoryArtifactStore(), LocalArtifactStore(tmp_path)):
        with pytest.raises(ValueError):
            await s.put(key, b"x")


def test_the_backend_comes_from_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("KEELCHAT_UPLOADS_BUCKET", raising=False)
    monkeypatch.delenv("KEELCHAT_DATA_ROOT", raising=False)
    assert artifact_store_from_env() is None
    monkeypatch.setenv("KEELCHAT_DATA_ROOT", str(tmp_path))
    assert isinstance(artifact_store_from_env(), LocalArtifactStore)
    monkeypatch.setenv("KEELCHAT_UPLOADS_BUCKET", "some-bucket")
    assert isinstance(artifact_store_from_env(), S3ArtifactStore)


async def test_knowledge_documents_are_kept_in_the_artifact_store(tmp_path):
    from keelchat.knowledge.docstore import ArtifactDocumentStore
    from keelchat.knowledge.models import Document

    files = LocalArtifactStore(tmp_path)
    store = ArtifactDocumentStore(files)
    shared, mine = Document.create("Corpus", "shared text"), Document.create("FAQ", "dataset text", "faq.md", dataset="acme/faq")
    await store.put(shared)
    await store.put(mine)
    await store.put(mine)  # re-indexing replaces the file
    assert [d.id for d in await store.list("")] == [shared.id]  # the shared folder does not descend into datasets/
    assert await store.ids("acme/faq") == {mine.id} and await store.count() == 2
    assert (await store.get(mine.id, "acme/faq")).title == "FAQ" and await store.get(mine.id) is None
    assert (tmp_path / "knowledge/docs" / f"{shared.id}.json").is_file()
    await store.delete(shared.id)
    await store.delete(mine.id, "acme/faq")
    assert await store.count() == 0

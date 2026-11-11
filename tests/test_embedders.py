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


def not_an_embedder() -> object:
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

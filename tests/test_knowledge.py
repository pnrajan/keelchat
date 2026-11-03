from __future__ import annotations

import pytest

from tests.helpers import RecordingLLM, make_runtime, reach_form, say
from yachat.knowledge.chunker import SemanticChunker, split_sentences
from yachat.knowledge.docstore import InMemoryDocumentStore
from yachat.knowledge.embedder import Embedder, FakeEmbedder
from yachat.knowledge.models import Document
from yachat.knowledge.service import KnowledgeService, make_indexer
from yachat.knowledge.vectorstore import InMemoryIndex
from yachat.runtime.llm import LLMRequest, ModelConfig
from yachat.runtime.llm_factory import llm_from_env

DOCS = [
    Document.create("Benefits", "We offer health insurance, dental coverage and a generous 401k match for every employee. " * 30, "hr/benefits"),
    Document.create("Remote work", "Most engineering roles are hybrid with three office days. Some analyst roles are fully remote. " * 30, "hr/remote"),
    Document.create("Commute", "The Austin office sits ten minutes from the 78704 zip code by bus. Parking is free. " * 30, "hr/office"),
]


def _llm() -> RecordingLLM:
    return RecordingLLM(llm_from_env())


SPORTS = "The striker scored twice in the final. The goalkeeper saved a penalty. Fans sang in the stadium all night. The league trophy returned to the city."
COOKING = "Whisk the eggs with sugar until pale. Fold in the sifted flour gently. Bake the sponge for twenty minutes. Let the cake cool before icing it."


class TopicEmbedder(Embedder):
    """Two axes, sport and food: the embedding-distance breakpoint is deterministic."""

    dim = 2

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if any(w in t.lower() for w in ("striker", "goalkeeper", "fans", "trophy")) else [0.0, 1.0] for t in texts]


async def test_semantic_chunker_breaks_on_topic_and_paragraph_never_inside_a_sentence():
    chunks = await SemanticChunker(FakeEmbedder(), max_words=200, min_words=10).chunk(f"{SPORTS}\n\n{COOKING}")
    assert any(c.endswith("city.") for c in chunks) and any(c.startswith("Whisk") for c in chunks)  # paragraph break honoured
    chunks = await SemanticChunker(TopicEmbedder(), max_words=200, min_words=10).chunk(f"{SPORTS} {COOKING}")
    assert chunks == [SPORTS, COOKING]  # no paragraph break: the embedding distance finds the topic shift
    sentences = [s for s, _ in split_sentences(SPORTS + " " + COOKING)]
    assert len(sentences) == 8 and " ".join(sentences) == " ".join(chunks)


async def test_semantic_chunker_caps_size_with_whole_sentences():
    text = "This sentence repeats to fill the document with words. " * 60
    chunks = await SemanticChunker(FakeEmbedder(), max_words=50).chunk(text)
    assert len(chunks) == 12 and all(len(c.split()) <= 50 and c.endswith("words.") for c in chunks)


@pytest.mark.parametrize("strategy", ["simple", "contextual", "raptor", "full_context"])
async def test_strategies_index_and_search(strategy):
    llm = _llm()
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer(strategy, FakeEmbedder(), llm, ModelConfig.from_env()))
    n = sum([await svc.add(d) for d in DOCS])
    hits = await svc.search("dental coverage and 401k match", k=3)
    assert hits and hits[0].title == "Benefits"
    if strategy == "full_context":
        assert n == 0 and all(len(h.text) > 500 for h in hits)
    else:
        assert n >= 3 and await svc.index.count() == n
    if strategy == "contextual":
        assert llm.calls_for("contextualize") and hits[0].text.count("\n") >= 1  # model-written context prefixed to the chunk
    if strategy == "raptor":
        assert llm.calls_for("summarize") and await svc.index.count() > 3 * 2  # leaves + summaries
        levels = {h.chunk.level for h in await svc.index.search((await svc.embedder.embed(["summary"]))[0], k=50)}
        assert 1 in levels
    await svc.remove(DOCS[0].id)
    assert all(h.title != "Benefits" for h in await svc.search("dental coverage", k=3))


async def test_knowledge_service_backs_the_tool(policy_yaml):
    svc = KnowledgeService(InMemoryDocumentStore(), InMemoryIndex(), FakeEmbedder(), make_indexer("simple", FakeEmbedder()))
    for d in DOCS:
        await svc.add(d)
    from yachat.testing.fakes import FakeTools

    tools = FakeTools()
    impls = tools.impls()
    impls["knowledge_search"] = svc.tool_impl()
    from yachat.policy.compiler import build
    from yachat.runtime.runtime import Runtime

    rt = Runtime(llm_from_env(), impls, models=ModelConfig.from_env())
    await rt.register_policy(build(policy_yaml))
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "do you offer dental coverage?")
    assert "dental coverage" in r.outbound.text and "What is your full name?" in r.outbound.text
    v = next(i for i in r.outbound.items if i["type"] == "verification")["verification"]
    assert v["passed"] and v["plan"]["method"] and v["checks"] and v["rounds"] >= 1  # planned, checked against the passages


async def test_a_knowledge_answer_the_verifier_rejects_is_not_shown(policy_yaml, monkeypatch):
    from yachat.capabilities.nodes import knowledge
    from yachat.testing.fakes import FakeTools

    async def made_up(self, plan, checks, feedback):
        return "Yes: every employee gets free daily lunch and a company car."  # nothing in the passages says so

    monkeypatch.setattr(knowledge._Answer, "produce", made_up)
    tools = FakeTools(passages=[{"title": "Benefits", "text": "We offer health insurance, dental coverage and a 401k match.", "source": "hr"}])
    rt, _ = await make_runtime(policy_yaml, tools=tools)
    s = await rt.start_session("hiring-agent", "u1")
    await reach_form(rt, s.id)
    r = await say(rt, s.id, "do employees get free lunch?")
    v = next(i for i in r.outbound.items if i["type"] == "verification")["verification"]
    assert not v["passed"] and v["rounds"] == 3 and v["issues"]  # the decorator's verify budget: 2 re-dos
    assert "company car" not in r.outbound.text and knowledge.UNVERIFIED in r.outbound.text


async def test_lancedb_index(tmp_path):
    pytest.importorskip("lancedb")
    from yachat.knowledge.vectorstore import LanceDBIndex

    emb = FakeEmbedder(dim=16)
    svc = KnowledgeService(InMemoryDocumentStore(), LanceDBIndex(str(tmp_path / "lance"), dim=16), emb, make_indexer("simple", FakeEmbedder()))
    for d in DOCS:
        await svc.add(d)
    assert await svc.index.count() >= 3
    hits = await svc.search("free parking near the Austin office", k=2)
    assert hits[0].title == "Commute"
    await svc.add(DOCS[2])  # re-index is idempotent
    before = await svc.index.count()
    await svc.remove(DOCS[2].id)
    assert await svc.index.count() < before


async def test_lancedb_filters_by_dataset_and_upgrades_old_tables(tmp_path):
    lancedb = pytest.importorskip("lancedb")
    from yachat.knowledge.models import Chunk
    from yachat.knowledge.vectorstore import LanceDBIndex

    uri = str(tmp_path / "lance")
    old = {"id": "a:0", "doc_id": "a", "title": "Old", "text": "t", "context": "", "level": 0, "strategy": "simple", "source": "", "vector": [1.0, 0.0]}
    lancedb.connect(uri).create_table("chunks", data=[old])  # a table from before datasets
    index = LanceDBIndex(uri, dim=2)
    assert [h.chunk.title for h in await index.search([1.0, 0.0])] == ["Old"]  # old chunks are the shared corpus
    await index.upsert([Chunk(id="b:0", doc_id="b", title="Acme", text="t", dataset="acme/x")], [[1.0, 0.0]])
    assert [h.chunk.title for h in await index.search([1.0, 0.0], k=5, dataset="acme/x")] == ["Acme"]
    assert [h.chunk.title for h in await index.search([1.0, 0.0], k=5)] == ["Old"]
    assert await index.search([1.0, 0.0], dataset="it's/x") == []  # quoted, not injected


async def test_litellm_client_structured(monkeypatch):
    from yachat.runtime.litellm_client import LiteLLMClient

    class Msg:
        content = "hi"
        tool_calls = [type("TC", (), {"function": type("F", (), {"arguments": '{"verdict": "answered"}'})()})()]

    class Resp:
        choices = [type("C", (), {"message": Msg()})()]

    async def fake_acompletion(**kwargs):
        fake_acompletion.kwargs = kwargs
        return Resp()

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    c = LiteLLMClient(api_key="k", api_base="http://x")
    req = LLMRequest("answer_check", "openai/Qwen", "sys", [{"role": "user", "content": "x"}])
    assert await c.complete(req) == "hi"
    assert (await c.structured(req, {"type": "object"}))["verdict"] == "answered"
    assert fake_acompletion.kwargs["tool_choice"]["function"]["name"] == "emit_answer_check"

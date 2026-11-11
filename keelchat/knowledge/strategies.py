"""Indexing strategies. Each turns a Document into Chunks to embed; `full_context` indexes nothing.
Leaf text comes from the chunker (by default the SemanticChunker: whole sentences, topic breakpoints)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from keelchat.knowledge.chunker import Chunker
from keelchat.knowledge.embedder import Embedder
from keelchat.knowledge.models import Chunk, Document, Strategy
from keelchat.knowledge.plugins import Plugins
from keelchat.runtime.llm import LLMClient, LLMRequest, ModelConfig


class Indexer(ABC):
    """Turns a document into the chunks to embed (or none, for `full_context`). Register yours with `@indexer("name")`:
    a function taking an `IndexContext`, or a class built as `cls(ctx)`. A knowledge base then uses it with
    `strategy: name`, or every one with KNOWLEDGE_STRATEGY=name. Set `retrieval = "whole_documents"` for a method that
    indexes nothing and answers from whole documents (as `full_context` does)."""

    name: Strategy
    #: how a search reads what this method indexed: nearest vectors, or whole documents within the prompt budget
    retrieval: Literal["vectors", "whole_documents"] = "vectors"

    @abstractmethod
    async def chunks(self, doc: Document) -> list[Chunk]: ...


@dataclass(frozen=True)
class IndexContext:
    """What an indexing method may use: the embedder, the chunker, and a model for summaries (None when none is
    configured; methods that need one raise)."""

    embedder: Embedder
    chunker: Chunker
    llm: LLMClient | None = None
    models: ModelConfig | None = None

    def need_llm(self, name: str) -> tuple[LLMClient, ModelConfig]:
        if self.llm is None:
            raise ValueError(f"strategy {name!r} needs a model (an LLMClient)")
        return self.llm, self.models or ModelConfig()


indexers: Plugins[Indexer] = Plugins("indexer", "strategy", Indexer)
indexer = indexers.register


class SimpleIndexer(Indexer):
    """Semantic chunks, embedded as they are."""

    name: Strategy = "simple"

    def __init__(self, chunker: Chunker):
        self.chunker = chunker

    async def chunks(self, doc: Document) -> list[Chunk]:
        return [
            Chunk(id=f"{doc.id}:{i}", doc_id=doc.id, title=doc.title, text=t, strategy=self.name, source=doc.source)
            for i, t in enumerate(await self.chunker.chunk(doc.text))
        ]


class ContextualIndexer(Indexer):
    """Contextual retrieval: a small model writes a short context for each chunk situating it in the whole document.
    The context is stored with the chunk and embedded with it."""

    name: Strategy = "contextual"

    def __init__(self, llm: LLMClient, models: ModelConfig, chunker: Chunker, max_doc_chars: int = 60_000):
        self.llm, self.models, self.chunker, self.max_doc_chars = llm, models, chunker, max_doc_chars

    async def _context(self, doc: Document, chunk: str) -> str:
        system = (
            "You situate a chunk within its document for search. Given the whole document and one chunk, write 1-2 sentences of "
            "context (what the chunk is about, where it sits, key entities) that help retrieve it. Output only the context."
        )
        content = f"<document>\n{doc.text[: self.max_doc_chars]}\n</document>\n<chunk>\n{chunk}\n</chunk>"
        req = LLMRequest(
            "contextualize",
            self.models.classify,
            system,
            [{"role": "user", "content": content}],
            max_tokens=150,
            effort=self.models.effort_classify,
            metadata={"title": doc.title, "chunk": chunk},
        )
        return (await self.llm.complete(req)).strip()

    async def chunks(self, doc: Document) -> list[Chunk]:
        out = []
        for i, t in enumerate(await self.chunker.chunk(doc.text)):
            ctx = await self._context(doc, t)
            out.append(Chunk(id=f"{doc.id}:{i}", doc_id=doc.id, title=doc.title, text=t, context=ctx, strategy=self.name, source=doc.source))
        return out


class RaptorIndexer(Indexer):
    """RAPTOR: leaf chunks, then recursive summaries of groups of neighbours until one root remains.
    Every level is indexed, so a query can match a detail or a theme. Grouping is by adjacency (no clustering dependency)."""

    name: Strategy = "raptor"

    def __init__(self, llm: LLMClient, models: ModelConfig, chunker: Chunker, fanout: int = 4, max_levels: int = 4):
        self.llm, self.models, self.chunker, self.fanout, self.max_levels = llm, models, chunker, fanout, max_levels

    async def _summarize(self, doc: Document, texts: list[str]) -> str:
        system = "Summarize the passages into one faithful paragraph that keeps names, numbers and decisions. Output only the summary."
        req = LLMRequest(
            "summarize",
            self.models.classify,
            system,
            [{"role": "user", "content": "\n\n---\n\n".join(texts)}],
            max_tokens=300,
            effort=self.models.effort_classify,
            metadata={"title": doc.title, "n": len(texts)},
        )
        return (await self.llm.complete(req)).strip()

    async def chunks(self, doc: Document) -> list[Chunk]:
        leaves = [
            Chunk(id=f"{doc.id}:0:{i}", doc_id=doc.id, title=doc.title, text=t, level=0, strategy=self.name, source=doc.source)
            for i, t in enumerate(await self.chunker.chunk(doc.text))
        ]
        out, current, level = list(leaves), leaves, 0
        while len(current) > 1 and level < self.max_levels:
            level += 1
            nxt = []
            for j in range(0, len(current), self.fanout):
                group = current[j : j + self.fanout]
                summary = await self._summarize(doc, [c.text for c in group])
                nxt.append(
                    Chunk(
                        id=f"{doc.id}:{level}:{j // self.fanout}",
                        doc_id=doc.id,
                        title=doc.title,
                        text=summary,
                        level=level,
                        context=f"Summary of {len(group)} passages (level {level})",
                        strategy=self.name,
                        source=doc.source,
                    )
                )
            out += nxt
            current = nxt
        return out


class FullContextIndexer(Indexer):
    """No vectors: retrieval returns whole documents that fit the prompt budget (small corpora, long-context models)."""

    name: Strategy = "full_context"
    retrieval = "whole_documents"

    async def chunks(self, doc: Document) -> list[Chunk]:
        return []


indexer("simple")(lambda ctx: SimpleIndexer(ctx.chunker))


@indexer("contextual")
def _contextual(ctx: IndexContext) -> Indexer:
    llm, models = ctx.need_llm("contextual")
    return ContextualIndexer(llm, models, ctx.chunker)


@indexer("raptor")
def _raptor(ctx: IndexContext) -> Indexer:
    llm, models = ctx.need_llm("raptor")
    return RaptorIndexer(llm, models, ctx.chunker)


indexer("full_context")(lambda ctx: FullContextIndexer())

"""Indexing strategies. Each turns a Document into Chunks to embed; `full_context` indexes nothing.
Leaf text comes from the SemanticChunker (whole sentences, topic breakpoints), never from a word count."""

from __future__ import annotations

from abc import ABC, abstractmethod

from keelchat.knowledge.chunker import SemanticChunker
from keelchat.knowledge.models import Chunk, Document, Strategy
from keelchat.runtime.llm import LLMClient, LLMRequest, ModelConfig


class Indexer(ABC):
    name: Strategy

    @abstractmethod
    async def chunks(self, doc: Document) -> list[Chunk]: ...


class SimpleIndexer(Indexer):
    """Semantic chunks, embedded as they are."""

    name: Strategy = "simple"

    def __init__(self, chunker: SemanticChunker):
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

    def __init__(self, llm: LLMClient, models: ModelConfig, chunker: SemanticChunker, max_doc_chars: int = 60_000):
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

    def __init__(self, llm: LLMClient, models: ModelConfig, chunker: SemanticChunker, fanout: int = 4, max_levels: int = 4):
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

    async def chunks(self, doc: Document) -> list[Chunk]:
        return []

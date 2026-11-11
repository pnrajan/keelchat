"""Semantic chunking: whole sentences, grouped where their embeddings stay close, capped by size.

A chunk never starts or ends inside a sentence, let alone a word. Breakpoints are the sentence gaps whose
embedding distance is above a percentile of all gaps in the document (adaptive, so it works for any embedding
model), plus paragraph breaks; a size cap keeps chunks within the embedding model's comfortable window.
"""

from __future__ import annotations

import math
import os
import re
from abc import ABC, abstractmethod

from keelchat.knowledge.embedder import Embedder
from keelchat.knowledge.plugins import Plugins

_SENTENCE_GAP = re.compile(r"(?<=[.!?…。])\s+(?=\S)")
_PARAGRAPH_GAP = re.compile(r"\n\s*\n")


def split_sentences(text: str) -> list[tuple[str, bool]]:
    """(sentence, starts_a_paragraph) pairs; whitespace normalised, nothing else changed."""
    out: list[tuple[str, bool]] = []
    for para in _PARAGRAPH_GAP.split(text):
        para = " ".join(para.split())
        if not para:
            continue
        for i, s in enumerate(_SENTENCE_GAP.split(para)):
            out.append((s, i == 0))
    return out


def _distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na, nb = math.sqrt(sum(x * x for x in a)), math.sqrt(sum(y * y for y in b))
    return 1.0 - dot / (na * nb) if na and nb else 1.0


class Chunker(ABC):
    """Splits a document's text into passages to embed and search. Register yours with `@chunker("name")` (built as
    `cls(embedder)`) and set KNOWLEDGE_CHUNKER=name."""

    @abstractmethod
    async def chunk(self, text: str) -> list[str]: ...


chunkers: Plugins[Chunker] = Plugins("chunker", "KNOWLEDGE_CHUNKER", Chunker)
chunker = chunkers.register


@chunker("semantic")
class SemanticChunker(Chunker):
    def __init__(self, embedder: Embedder, *, max_words: int = 200, min_words: int = 40, breakpoint_percentile: int = 90):
        self.embedder, self.max_words, self.min_words, self.breakpoint_percentile = embedder, max_words, min_words, breakpoint_percentile

    async def chunk(self, text: str) -> list[str]:
        sentences = split_sentences(text)
        if not sentences:
            return []
        vectors = await self.embedder.embed([s for s, _ in sentences])
        gaps = [_distance(vectors[i], vectors[i + 1]) for i in range(len(sentences) - 1)]
        cut = sorted(gaps)[int(self.breakpoint_percentile / 100 * (len(gaps) - 1))] if gaps else 0.0
        chunks: list[str] = []
        current: list[str] = []
        words = 0
        for i, (sentence, new_paragraph) in enumerate(sentences):
            n = len(sentence.split())
            topic_shift = i > 0 and (gaps[i - 1] > cut or new_paragraph)
            if current and (words + n > self.max_words or (topic_shift and words >= self.min_words)):
                chunks.append(" ".join(current))
                current, words = [], 0
            current.append(sentence)
            words += n
        if current:
            chunks.append(" ".join(current))
        return chunks


@chunker("sentences")
class SentenceWindowChunker(Chunker):
    """Whole sentences, up to `max_words` a passage, starting afresh at each paragraph once `min_words` is reached. No
    embedding calls: cheaper to index than `semantic`, with breaks only where the writer put them."""

    def __init__(self, embedder: Embedder | None = None, *, max_words: int = 200, min_words: int = 40):
        self.max_words, self.min_words = max_words, min_words

    async def chunk(self, text: str) -> list[str]:
        chunks: list[str] = []
        current: list[str] = []
        words = 0
        for sentence, new_paragraph in split_sentences(text):
            n = len(sentence.split())
            if current and (words + n > self.max_words or (new_paragraph and words >= self.min_words)):
                chunks.append(" ".join(current))
                current, words = [], 0
            current.append(sentence)
            words += n
        if current:
            chunks.append(" ".join(current))
        return chunks


def chunker_from_env(embedder: Embedder) -> Chunker:
    """KNOWLEDGE_CHUNKER: `chonkie` (the default when chonkie is installed), `semantic` (built in: the embedder
    finds the breaks), `sentences`, a registered name or `package.module:Name`. For chonkie, KNOWLEDGE_CHONKIE picks
    its chunker (semantic, recursive, sentence, token) and KNOWLEDGE_CHUNK_SIZE caps a passage in tokens."""
    import importlib.util

    choice = os.getenv("KNOWLEDGE_CHUNKER") or ("chonkie" if importlib.util.find_spec("chonkie") else "semantic")
    if choice == "chonkie":
        return chunkers.make(
            "chonkie",
            embedder,
            kind=os.getenv("KNOWLEDGE_CHONKIE") or "semantic",
            chunk_size=int(os.getenv("KNOWLEDGE_CHUNK_SIZE") or 512),
            **({"model": m} if (m := os.getenv("KNOWLEDGE_CHONKIE_MODEL")) else {}),
        )
    return chunkers.make(choice, embedder)

"""chonkie (https://github.com/chonkie-inc/chonkie) as a chunker and an embedder. Needs `pip install "chonkie[semantic]"`
(included in the `api` and `knowledge` extras): small, no PyTorch; its default model2vec models run on CPU.

- `KNOWLEDGE_CHUNKER=chonkie`: chonkie's semantic chunker (breaks where meaning shifts, measured with a small local
  model), or `KNOWLEDGE_CHONKIE=recursive|sentence|token`. `KNOWLEDGE_CHUNK_SIZE` caps a passage in tokens.
- `KNOWLEDGE_EMBEDDER=chonkie`: any embedding model chonkie loads (`minishlab/potion-base-8M` by default, or a
  sentence-transformers, OpenAI, Cohere, Voyage, Jina or Gemini model by name).
"""

from __future__ import annotations

import asyncio
import importlib.util
from typing import Any, Literal

from keelchat.knowledge.chunker import Chunker, chunker
from keelchat.knowledge.embedder import Embedder, embedder

#: a small static-embedding model: fast on CPU, about 30 MB
DEFAULT_MODEL = "minishlab/potion-base-8M"
ChonkieKind = Literal["semantic", "recursive", "sentence", "token"]


def installed() -> bool:
    return importlib.util.find_spec("chonkie") is not None


def _need() -> Any:
    try:
        import chonkie
    except ImportError as e:
        raise RuntimeError('chonkie is not installed: pip install "chonkie[semantic]"') from e
    return chonkie


@chunker("chonkie")
class ChonkieChunker(Chunker):
    """Passages from one of chonkie's chunkers. `semantic` breaks where the meaning shifts (similarity between
    neighbouring sentences under `threshold`); the others split on structure, sentences or tokens."""

    def __init__(
        self,
        embedder: Embedder | None = None,
        *,
        kind: ChonkieKind = "semantic",
        chunk_size: int = 512,
        model: str = DEFAULT_MODEL,
        threshold: float = 0.7,
    ):
        _need()
        if kind not in ("semantic", "recursive", "sentence", "token"):
            raise ValueError(f"unknown chonkie chunker {kind!r}; use semantic, recursive, sentence or token")
        self.kind, self.chunk_size, self.model, self.threshold = kind, chunk_size, model, threshold
        self._chunker: Any = None  # built on first use: the semantic one loads a model

    def _built(self) -> Any:
        if self._chunker is None:
            lib = _need()
            if self.kind == "semantic":
                self._chunker = lib.SemanticChunker(embedding_model=self.model, chunk_size=self.chunk_size, threshold=self.threshold)
            elif self.kind == "recursive":
                self._chunker = lib.RecursiveChunker(chunk_size=self.chunk_size)
            elif self.kind == "sentence":
                self._chunker = lib.SentenceChunker(chunk_size=self.chunk_size)
            else:
                self._chunker = lib.TokenChunker(chunk_size=self.chunk_size)
        return self._chunker

    async def chunk(self, text: str) -> list[str]:
        if not text.strip():
            return []
        chunks = await asyncio.to_thread(lambda: self._built().chunk(text))  # loading and CPU work off the event loop
        return [t for c in chunks if (t := c.text.strip())]


@embedder("chonkie")
class ChonkieEmbedder(Embedder):
    """An embedding model loaded by chonkie's AutoEmbeddings: a model2vec model by default (local, no key)."""

    def __init__(self, model: str | None = None):
        _need()
        self.model = model or DEFAULT_MODEL
        self._emb: Any = None  # loaded on first use, so a server that never indexes never loads it
        self.dim = 0

    def _loaded(self) -> Any:
        if self._emb is None:
            self._emb = _need().AutoEmbeddings.get_embeddings(self.model)
            self.dim = int(self._emb.dimension)
        return self._emb

    @property
    def id(self) -> str:
        return f"chonkie:{self.model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = await asyncio.to_thread(lambda: self._loaded().embed_batch(texts))
        return [[float(x) for x in v] for v in vectors]

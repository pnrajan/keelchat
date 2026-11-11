"""Embedder port: turns text into vectors for knowledge-base search.

Built in: any embedding model litellm reaches (`litellm`), sentence-transformers models run locally
(`sentence-transformers`), SiliconFlow (`siliconflow`), and a word-hashing fallback (`hash`). Add your own with
`@embedder("name")` on a subclass of `Embedder` (or a function returning one) in a capability package, then set
`KNOWLEDGE_EMBEDDER=name`; or point `KNOWLEDGE_EMBEDDER` at it directly as `package.module:Name`.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import math
import os
import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from keelchat.knowledge.plugins import Plugins
from keelchat.logging import get_logger

log = get_logger("embedder")


class Embedder(ABC):
    """Turns texts into vectors of one fixed size. Similar meanings must land near each other (cosine)."""

    #: vector size; 0 until known (some providers tell it only on the first call)
    dim: int = 0

    @property
    def id(self) -> str:
        """What made the vectors (kind and model): vectors from different embedders never share an index table."""
        return type(self).__name__

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


def _build(factory: Callable[..., Embedder], model: str | None) -> Embedder:
    """A class gets the model only when one is set (so its own default applies); a function always gets it."""
    made: Embedder = (factory(model) if model else factory()) if isinstance(factory, type) else factory(model)
    return made


#: name → an Embedder subclass or a function `(model: str | None) -> Embedder`; filled by @embedder
embedders: Plugins[Embedder] = Plugins("embedder", "KNOWLEDGE_EMBEDDER", Embedder, call=_build)
EMBEDDERS = embedders.factories
#: register an embedder under a name: an `Embedder` subclass (built as `cls(model)` when KNOWLEDGE_EMBED_MODEL is set,
#: else `cls()`) or a function `(model: str | None) -> Embedder`
embedder = embedders.register


class FakeEmbedder(Embedder):
    """Bag-of-words hashed into a fixed-size vector: similar words land near each other, deterministically. No model,
    no key: tests, and a fallback that matches words rather than meaning."""

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.calls = 0

    @property
    def id(self) -> str:
        return f"hash:{self.dim}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        out = []
        for t in texts:
            v = [0.0] * self.dim
            for w in re.findall(r"[a-z0-9áéíóúñü]+", t.lower()):
                if len(w) <= 2:
                    continue
                h = int(hashlib.md5(w.encode()).hexdigest(), 16)  # noqa: S324 - not security
                v[h % self.dim] += 1.0
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


embedder("hash")(lambda model: FakeEmbedder())  # no model to choose


#: vector sizes of common models, so the size is known before the first call
KNOWN_DIMS = {
    "openai/text-embedding-3-small": 1536,
    "openai/text-embedding-3-large": 3072,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "voyage/voyage-3": 1024,
    "voyage/voyage-3-lite": 512,
    "cohere/embed-english-v3.0": 1024,
    "cohere/embed-multilingual-v3.0": 1024,
    "gemini/text-embedding-004": 768,
    "mistral/mistral-embed": 1024,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "sentence-transformers/all-mpnet-base-v2": 768,
    "BAAI/bge-small-en-v1.5": 384,
}


@embedder("litellm")
class LiteLLMEmbedder(Embedder):
    """Any embedding model litellm reaches, named the litellm way: `openai/text-embedding-3-small`,
    `voyage/voyage-3`, `cohere/embed-english-v3.0`, `gemini/text-embedding-004`, `bedrock/amazon.titan-embed-text-v2:0`,
    `mistral/mistral-embed`, `ollama/nomic-embed-text`. The provider's key is read the usual litellm way
    (OPENAI_API_KEY, VOYAGE_API_KEY, ...); KNOWLEDGE_EMBED_API_BASE points at a compatible server."""

    def __init__(self, model: str | None = None, api_key: str | None = None, api_base: str | None = None, dim: int = 0, batch: int = 64):
        self.model = model or os.getenv("KNOWLEDGE_EMBED_MODEL") or "openai/text-embedding-3-small"
        self.api_key, self.api_base, self.batch = api_key, api_base or os.getenv("KNOWLEDGE_EMBED_API_BASE"), batch
        self.dim = dim or int(os.getenv("KNOWLEDGE_EMBED_DIM") or 0) or KNOWN_DIMS.get(self.model, 0)

    @property
    def id(self) -> str:
        return f"litellm:{self.model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import litellm

        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            resp: Any = await litellm.aembedding(
                model=self.model,
                input=texts[i : i + self.batch],
                **({"api_key": self.api_key} if self.api_key else {}),
                **({"api_base": self.api_base} if self.api_base else {}),
                timeout=30.0,  # a stalled request fails instead of hanging indexing or search
                num_retries=2,
            )
            out.extend([list(map(float, d["embedding"])) for d in resp["data"]])
        if out:
            self.dim = len(out[0])
        log.info("embedded", n=len(texts), model=self.model)
        return out


@embedder("siliconflow")
class SiliconFlowEmbedder(LiteLLMEmbedder):
    """SiliconFlow's OpenAI-compatible embeddings endpoint (Qwen3 embeddings by default)."""

    def __init__(self, model: str | None = None, api_key: str | None = None, api_base: str | None = None, dim: int = 1024, batch: int = 32):
        key = api_key or os.getenv("SILICONFLOW_API_KEY")
        if not key:
            raise RuntimeError("SILICONFLOW_API_KEY is required for SiliconFlowEmbedder")
        name = model or os.getenv("KNOWLEDGE_EMBED_MODEL") or "Qwen/Qwen3-Embedding-0.6B"
        base = api_base or os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.com/v1"
        super().__init__(f"openai/{name}", key, base, dim, batch)
        self.name = name

    @property
    def id(self) -> str:
        return f"siliconflow:{self.name}"


@embedder("sentence-transformers")
class SentenceTransformerEmbedder(Embedder):
    """A sentence-transformers model run on this machine: no key, no network after the first download. Needs
    `pip install sentence-transformers` (it brings PyTorch; about 1 GB of memory for small models)."""

    def __init__(self, model: str | None = None, device: str | None = None, batch: int = 32):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise RuntimeError("KNOWLEDGE_EMBEDDER=sentence-transformers needs `pip install sentence-transformers`") from e
        self.model_name = model or os.getenv("KNOWLEDGE_EMBED_MODEL") or "sentence-transformers/all-MiniLM-L6-v2"
        self.batch = batch
        self._model = SentenceTransformer(self.model_name, device=device)
        self.dim = int(self._model.get_sentence_embedding_dimension() or 0)

    @property
    def id(self) -> str:
        return f"sentence-transformers:{self.model_name}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = await asyncio.to_thread(self._model.encode, texts, batch_size=self.batch, normalize_embeddings=True)
        return [list(map(float, v)) for v in vectors]


def _sentence_transformers_installed() -> bool:
    return importlib.util.find_spec("sentence_transformers") is not None


def embedder_from_env() -> Embedder:
    """KNOWLEDGE_EMBEDDER: a registered name (`litellm`, `sentence-transformers`, `siliconflow`, `hash`, or one from
    a capability package) or an import path `package.module:Name`; KNOWLEDGE_EMBED_MODEL picks its model. Unset: the
    first that works of SiliconFlow (SILICONFLOW_API_KEY), an embedding model named in KNOWLEDGE_EMBED_MODEL or OpenAI's
    (OPENAI_API_KEY), sentence-transformers (when installed), and the word-hashing fallback."""
    choice, model = os.getenv("KNOWLEDGE_EMBEDDER", "").strip(), os.getenv("KNOWLEDGE_EMBED_MODEL") or None
    if not choice:
        if os.getenv("SILICONFLOW_API_KEY"):
            choice = "siliconflow"
        elif model or os.getenv("OPENAI_API_KEY"):
            choice = "litellm"
        elif _sentence_transformers_installed():
            choice = "sentence-transformers"
        else:
            log.warning("no embedding model configured: knowledge search matches words, not meaning (set KNOWLEDGE_EMBEDDER)")
            return FakeEmbedder()
    try:
        made = embedders.make(choice, model)
    except ValueError as e:
        raise RuntimeError(str(e)) from None
    log.info("embedder", embedder=made.id)
    return made


def index_table(e: Embedder) -> str:
    """The index table for this embedder's vectors. The default SiliconFlow model keeps the original name."""
    if e.id == "siliconflow:Qwen/Qwen3-Embedding-0.6B":
        return "chunks"
    return "chunks_" + hashlib.sha256(e.id.encode()).hexdigest()[:10]

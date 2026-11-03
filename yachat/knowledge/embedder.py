"""Embedder port. SiliconFlow through litellm; a deterministic fake for tests."""

from __future__ import annotations

import hashlib
import math
import os
import re
from abc import ABC, abstractmethod
from typing import Any

from yachat.logging import get_logger

log = get_logger("embedder")


class Embedder(ABC):
    dim: int

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbedder(Embedder):
    """Bag-of-words hashed into a fixed-size vector: similar texts land near each other, deterministically."""

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.calls = 0

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


class SiliconFlowEmbedder(Embedder):
    """SiliconFlow's OpenAI-compatible embeddings endpoint, called through litellm."""

    def __init__(self, model: str | None = None, api_key: str | None = None, api_base: str | None = None, dim: int = 1024, batch: int = 32):
        self.model = model or (os.getenv("KNOWLEDGE_EMBED_MODEL") or "Qwen/Qwen3-Embedding-0.6B")
        self.api_key = api_key or os.getenv("SILICONFLOW_API_KEY")
        self.api_base = api_base or (os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.com/v1")
        if not self.api_key:
            raise RuntimeError("SILICONFLOW_API_KEY is required for SiliconFlowEmbedder")
        self.dim, self.batch = dim, batch

    async def embed(self, texts: list[str]) -> list[list[float]]:
        import litellm

        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            resp: Any = await litellm.aembedding(
                model=f"openai/{self.model}",
                input=texts[i : i + self.batch],
                api_key=self.api_key,
                api_base=self.api_base,
                timeout=30.0,  # a stalled request fails instead of hanging indexing or search
                num_retries=2,
            )
            out.extend([list(map(float, d["embedding"])) for d in resp["data"]])
        log.info("embedded", n=len(texts), model=self.model)
        return out

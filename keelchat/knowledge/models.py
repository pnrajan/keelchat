from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field

Strategy = Literal["simple", "contextual", "raptor", "full_context"]


def doc_id_for(source: str, text: str) -> str:
    return hashlib.sha256(f"{source}\n{text}".encode()).hexdigest()[:16]


class Document(BaseModel):
    id: str
    title: str
    text: str
    source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    #: the dataset ("tenant/name") this document belongs to; "" is the shared corpus
    dataset: str = ""

    @classmethod
    def create(cls, title: str, text: str, source: str = "", metadata: dict[str, Any] | None = None, dataset: str = "") -> Document:
        key = f"{dataset}|{source or title}" if dataset else source or title  # one file in two datasets is two documents
        return cls(id=doc_id_for(key, text), title=title, text=text, source=source, metadata=metadata or {}, dataset=dataset)


class Chunk(BaseModel):
    id: str
    doc_id: str
    title: str
    text: str
    #: contextual-retrieval prefix or RAPTOR summary parentage; stored with the chunk, embedded with it
    context: str = ""
    #: 0 = leaf chunk; RAPTOR summaries have level >= 1
    level: int = 0
    strategy: Strategy = "simple"
    source: str = ""
    #: copied from its document; searches filter on it
    dataset: str = ""

    @property
    def embed_text(self) -> str:
        return f"{self.context}\n\n{self.text}".strip() if self.context else self.text


class Hit(BaseModel):
    chunk: Chunk
    score: float


class Passage(BaseModel):
    title: str
    text: str
    source: str = ""
    score: float = 0.0

"""Document store port: one JSON file per document in the artifact store, and an in-memory fake.

Documents are kept per scope: the shared corpus (`dataset=""`) and each dataset apart, so listing one never reads
another. The shared corpus is `<prefix><id>.json` and a dataset's documents are under
`<prefix>datasets/<dataset ref>/`. Listing ids and counting read file names only, never bodies."""

from __future__ import annotations

import builtins
import re
from abc import ABC, abstractmethod

from keelchat.knowledge.models import Document
from keelchat.state.artifacts import ArtifactStore

_SAFE = re.compile(r"[^A-Za-z0-9._/-]")


class DocumentStore(ABC):
    @abstractmethod
    async def put(self, doc: Document) -> None:
        """Stored under the document's own scope (`doc.dataset`)."""

    @abstractmethod
    async def get(self, doc_id: str, dataset: str = "") -> Document | None: ...

    @abstractmethod
    async def list(self, dataset: str = "") -> builtins.list[Document]:
        """The documents of one scope: the shared corpus (""), or one dataset."""

    @abstractmethod
    async def ids(self, dataset: str = "") -> set[str]:
        """The document ids of one scope, without reading the documents."""

    @abstractmethod
    async def count(self) -> int:
        """Documents in every scope, without reading them."""

    @abstractmethod
    async def delete(self, doc_id: str, dataset: str = "") -> None: ...


class InMemoryDocumentStore(DocumentStore):
    def __init__(self) -> None:
        self._d: dict[tuple[str, str], Document] = {}

    async def put(self, doc: Document) -> None:
        self._d[(doc.dataset, doc.id)] = doc

    async def get(self, doc_id: str, dataset: str = "") -> Document | None:
        return self._d.get((dataset, doc_id))

    async def list(self, dataset: str = "") -> builtins.list[Document]:
        return [d for (s, _), d in self._d.items() if s == dataset]

    async def ids(self, dataset: str = "") -> set[str]:
        return {i for (s, i) in self._d if s == dataset}

    async def count(self) -> int:
        return len(self._d)

    async def delete(self, doc_id: str, dataset: str = "") -> None:
        self._d.pop((dataset, doc_id), None)


class ArtifactDocumentStore(DocumentStore):
    """Documents as JSON files in the artifact store (S3 in production, a folder or memory otherwise). Re-indexing a
    document replaces its file."""

    def __init__(self, files: ArtifactStore, prefix: str = "knowledge/docs/"):
        self.files, self.prefix = files, prefix

    def _scope(self, dataset: str) -> str:
        return f"{self.prefix}datasets/{_SAFE.sub('_', dataset)}/" if dataset else self.prefix

    def _key(self, doc_id: str, dataset: str = "") -> str:
        return f"{self._scope(dataset)}{doc_id}.json"

    async def _keys(self, dataset: str) -> builtins.list[str]:
        """Keys directly in one scope (the shared corpus does not descend into datasets/)."""
        return [k for k in await self.files.keys(self._scope(dataset), recursive=bool(dataset)) if k.endswith(".json")]

    async def put(self, doc: Document) -> None:
        await self.files.put(self._key(doc.id, doc.dataset), doc.model_dump_json().encode(), content_type="application/json", replace=True)

    async def get(self, doc_id: str, dataset: str = "") -> Document | None:
        raw = await self.files.get(self._key(doc_id, dataset))
        return Document.model_validate_json(raw) if raw is not None else None

    async def list(self, dataset: str = "") -> builtins.list[Document]:
        docs = []
        for k in await self._keys(dataset):
            raw = await self.files.get(k)
            if raw is not None:
                docs.append(Document.model_validate_json(raw))
        return [d for d in docs if d.dataset == dataset]  # documents written before scopes existed sit in the shared folder

    async def ids(self, dataset: str = "") -> set[str]:
        return {k.rsplit("/", 1)[-1].removesuffix(".json") for k in await self._keys(dataset)}

    async def count(self) -> int:
        return len([k for k in await self.files.keys(self.prefix) if k.endswith(".json")])

    async def delete(self, doc_id: str, dataset: str = "") -> None:
        await self.files.delete(self._key(doc_id, dataset))

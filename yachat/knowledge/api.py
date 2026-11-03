"""Knowledge Service as a microservice: index documents and serve `POST /search`, the contract the runtime's
`knowledge_search` tool (KNOWLEDGE_URL) already speaks. Deployable on its own (the same Docker image)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field

from yachat.api.keys import require_service_key
from yachat.datasets.models import Dataset
from yachat.datasets.sources import SourceRejected
from yachat.knowledge.models import Document, Passage, Strategy
from yachat.knowledge.service import KnowledgeService, service_from_env
from yachat.logging import configure, get_logger
from yachat.runtime.llm import LLMClient, ModelConfig

log = get_logger("knowledge.api")


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocumentIn(Body):
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=2_000_000)
    source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class DocumentsIn(Body):
    documents: list[DocumentIn] = Field(min_length=1, max_length=200)


class Indexed(BaseModel):
    id: str
    title: str
    chunks: int


class SearchIn(Body):
    query: str = Field(min_length=1, max_length=4_000)
    k: int = Field(default=5, ge=1, le=50)
    #: search this dataset (indexed on first use) instead of the shared corpus
    dataset: Dataset | None = None


class SearchOut(BaseModel):
    results: list[Passage]


class Health(BaseModel):
    status: str = "ok"
    strategy: Strategy
    embedder: str
    dim: int
    documents: int
    chunks: int


def _llm_for(strategy: str) -> LLMClient | None:
    """contextual / raptor summarise with a model; simple / full_context need none."""
    if strategy not in ("contextual", "raptor"):
        return None
    from yachat.runtime.llm_factory import llm_from_env

    return llm_from_env()


def create_app(service: KnowledgeService | None = None) -> FastAPI:
    load_dotenv()
    configure()
    from yachat.tracing import init as init_tracing

    init_tracing("knowledge")
    svc = service or service_from_env(_llm_for(os.getenv("KNOWLEDGE_STRATEGY") or "simple"), ModelConfig.from_env())

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await svc.reindex_if_empty()
        yield

    app = FastAPI(title="yachat Knowledge Service", version="0.1.0", dependencies=[Depends(require_service_key)], lifespan=lifespan)
    app.state.service = svc

    @app.get("/health", response_model=Health)
    async def health() -> Health:
        return Health(
            strategy=svc.strategy,
            embedder=type(svc.embedder).__name__,
            dim=svc.embedder.dim,
            documents=await svc.document_count(),
            chunks=await svc.index.count(),
        )

    @app.post("/documents", status_code=201, response_model=list[Indexed])
    async def add_documents(body: DocumentsIn) -> list[Indexed]:
        out = []
        for d in body.documents:
            doc = Document.create(d.title, d.text, d.source, metadata=d.metadata)
            out.append(Indexed(id=doc.id, title=doc.title, chunks=await svc.add(doc)))
        return out

    @app.get("/documents", response_model=list[Indexed])
    async def list_documents() -> list[Indexed]:
        """The shared corpus; a dataset's documents are managed through its sources (/datasets)."""
        return [Indexed(id=d.id, title=d.title, chunks=0) for d in await svc.docs.list("")]

    @app.delete("/documents/{doc_id}")
    async def delete_document(doc_id: str = Path(pattern=r"^[0-9a-f]{16}$")) -> dict[str, str]:
        if await svc.docs.get(doc_id) is None:
            raise HTTPException(404, f"document '{doc_id}'")
        await svc.remove(doc_id)
        return {"deleted": doc_id}

    @app.post("/search", response_model=SearchOut)
    async def search(body: SearchIn) -> SearchOut:
        try:
            return SearchOut(results=await svc.search(body.query, k=body.k, dataset=body.dataset))
        except (SourceRejected, OSError, ValueError) as e:
            log.error("dataset unavailable", dataset=body.dataset.ref if body.dataset else "", error=f"{e.__class__.__name__}: {e}")
            raise HTTPException(422, "dataset sources unavailable") from None

    @app.post("/datasets")
    async def index_dataset(body: Dataset) -> dict[str, Any]:
        """Index (or re-index) an unstructured dataset now, so registration surfaces unreadable sources."""
        try:
            return {"dataset": body.ref, "version": body.version, "chunks": await svc.index_dataset(body, register=True)}
        except (SourceRejected, OSError, ValueError) as e:
            log.error("dataset index failed", dataset=body.ref, error=f"{e.__class__.__name__}: {e}")
            raise HTTPException(422, str(e) if isinstance(e, SourceRejected | ValueError) else "source_unreadable") from None

    @app.delete("/datasets/{tenant}/{name}")
    async def drop_dataset(tenant: str, name: str, version: str | None = None) -> dict[str, Any]:
        """Purge a dataset's documents and vectors (the API calls this when the dataset is deleted)."""
        return {"dataset": f"{tenant}/{name}", "documents": await svc.drop_dataset(f"{tenant}/{name}", version)}

    return app

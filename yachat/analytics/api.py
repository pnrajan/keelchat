"""Analytics Service as a microservice: `POST /datasets` loads a structured dataset (registration checks it reads),
`POST /analyze` answers a question with an AnalysisResult. The runtime's `analytics_query` tool (ANALYTICS_URL) calls it."""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException

from yachat.analytics.duck import AnalyticsError
from yachat.analytics.models import AnalysisResult, AnalyzeIn
from yachat.analytics.service import AnalyticsService, service_from_env
from yachat.api.keys import require_service_key
from yachat.datasets.models import Dataset
from yachat.datasets.sources import SourceRejected
from yachat.logging import configure, get_logger
from yachat.runtime.llm import ModelConfig

log = get_logger("analytics.api")


def _reason(e: Exception) -> str:
    """Safe to return: a rejected source names the caller's own URI; other failures only their category."""
    if isinstance(e, SourceRejected):
        return str(e)
    return getattr(e, "kind", "source_unreadable")


def create_app(service: AnalyticsService | None = None) -> FastAPI:
    load_dotenv()
    configure()
    from yachat.tracing import init as init_tracing

    init_tracing("analytics")
    if service is None:
        from yachat.runtime.llm_factory import llm_from_env

        service = service_from_env(llm_from_env(), ModelConfig.from_env())
    svc = service
    app = FastAPI(title="yachat Analytics Service", version="0.1.0", dependencies=[Depends(require_service_key)])
    app.state.service = svc

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "datasets_loaded": len(svc._dbs)}

    @app.post("/datasets")
    async def load_dataset(body: Dataset) -> dict[str, Any]:
        try:
            tables = await svc.tables(body)
        except (AnalyticsError, SourceRejected, OSError) as e:
            log.error("dataset load failed", dataset=body.ref, error=f"{e.__class__.__name__}: {e}")
            raise HTTPException(422, _reason(e)) from None
        return {"dataset": body.ref, "version": body.version, "tables": [{"name": t.name, "rows": t.rows, "columns": dict(t.columns)} for t in tables]}

    @app.post("/analyze", response_model=AnalysisResult)
    async def analyze(body: AnalyzeIn) -> AnalysisResult:
        try:
            return await svc.analyze(body)
        except (AnalyticsError, SourceRejected, OSError) as e:
            log.error("dataset load failed", dataset=body.dataset.ref, error=f"{e.__class__.__name__}: {e}")
            raise HTTPException(422, _reason(e)) from None

    return app

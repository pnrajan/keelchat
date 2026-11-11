"""Analytics Service as a microservice: `POST /datasets` loads a structured dataset (registration checks it reads),
`POST /analyze` answers a question with an AnalysisResult. The runtime's `analytics_query` tool (ANALYTICS_URL) calls it."""

from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from pydantic import Field

from keelchat.analytics.duck import AnalyticsError
from keelchat.analytics.models import AnalysisResult, AnalyzeIn
from keelchat.analytics.service import AnalyticsService, service_from_env
from keelchat.api.keys import require_service_key
from keelchat.datasets.models import Dataset
from keelchat.datasets.sources import SourceRejected
from keelchat.logging import configure, get_logger
from keelchat.runtime.llm import ModelConfig
from keelchat.runtime.usage import Usage, metering

log = get_logger("analytics.api")


class AnalyzeOut(AnalysisResult):
    #: the model calls this answer made, priced here; the runtime adds them to the cost of the turn that asked
    usage: list[Usage] = Field(default_factory=list)


def _reason(e: Exception) -> str:
    """Safe to return: a rejected source names the caller's own URI; other failures only their category."""
    if isinstance(e, SourceRejected):
        return str(e)
    return getattr(e, "kind", "source_unreadable")


def create_app(service: AnalyticsService | None = None) -> FastAPI:
    load_dotenv()
    configure()
    from keelchat.tracing import init as init_tracing

    init_tracing("analytics")
    if service is None:
        from keelchat.runtime.llm_factory import llm_from_env

        service = service_from_env(llm_from_env(), ModelConfig.from_env())
    svc = service
    app = FastAPI(title="keelchat Analytics Service", version="0.1.0", dependencies=[Depends(require_service_key)])
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

    @app.post("/analyze", response_model=AnalyzeOut)
    async def analyze(body: AnalyzeIn) -> AnalyzeOut:
        try:
            with metering() as calls:
                result = await svc.analyze(body)
            return AnalyzeOut.model_validate(result.model_dump() | {"usage": calls})
        except (AnalyticsError, SourceRejected, OSError) as e:
            log.error("dataset load failed", dataset=body.dataset.ref, error=f"{e.__class__.__name__}: {e}")
            raise HTTPException(422, _reason(e)) from None

    return app

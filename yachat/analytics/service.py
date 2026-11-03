"""AnalyticsService: loads structured datasets (cached per version, bounded) and answers questions with the Analyst."""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict
from typing import Any

from yachat.analytics.analyst import Analyst, Limits
from yachat.analytics.duck import DatasetDB, TableInfo
from yachat.analytics.models import AnalysisResult, AnalyzeIn, HistoryItem
from yachat.datasets.models import Dataset
from yachat.logging import get_logger
from yachat.runtime.llm import LLMClient, ModelConfig

log = get_logger("analytics")


class AnalyticsService:
    def __init__(self, analyst: Analyst, *, max_datasets: int = 8):
        self.analyst = analyst
        self.max_datasets = max_datasets
        self._dbs: OrderedDict[str, DatasetDB] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    async def load(self, dataset: Dataset) -> DatasetDB:
        """The loaded database for this dataset version; a new version replaces the old one. Least recently used first out."""
        async with self._locks.setdefault(dataset.ref, asyncio.Lock()):
            db = self._dbs.get(dataset.ref)
            if db is not None and db.dataset is not None and db.dataset.version == dataset.version:
                self._dbs.move_to_end(dataset.ref)
                return db
            new = await asyncio.to_thread(DatasetDB, dataset)
            if db is not None:
                db.close()
            self._dbs[dataset.ref] = new
            self._dbs.move_to_end(dataset.ref)
            while len(self._dbs) > self.max_datasets:
                _, old = self._dbs.popitem(last=False)
                old.close()
            log.info("dataset loaded", dataset=dataset.ref, version=dataset.version, tables={t.name: t.rows for t in new.tables})
            return new

    async def tables(self, dataset: Dataset) -> list[TableInfo]:
        return (await self.load(dataset)).tables

    async def analyze(self, req: AnalyzeIn) -> AnalysisResult:
        return await self.analyst.analyze(req, await self.load(req.dataset))

    def tool_impl(self) -> Any:
        """An `analytics_query` implementation for Runtime(tool_impls=...): in-process, no HTTP."""

        async def analytics_query(
            question: str, dataset: Dataset, history: list[HistoryItem] | None = None, repairs: dict[str, int] | None = None
        ) -> AnalysisResult:
            return await self.analyze(AnalyzeIn(question=question, dataset=dataset, history=history or [], repairs=repairs or {}))

        return analytics_query


def limits_from_env() -> Limits:
    return Limits(
        row_cap=int(os.getenv("ANALYTICS_ROW_CAP") or 200),
        stats_cap=int(os.getenv("ANALYTICS_STATS_CAP") or 100_000),
        query_timeout_s=float(os.getenv("ANALYTICS_QUERY_TIMEOUT_S") or 20.0),
        code_timeout_s=float(os.getenv("ANALYTICS_CODE_TIMEOUT_S") or 60.0),
        max_repairs=int(os.getenv("ANALYTICS_MAX_REPAIRS") or 3),
    )


def service_from_env(llm: LLMClient, models: ModelConfig | None = None) -> AnalyticsService:
    from yachat.analytics.sandbox import sandbox_from_env

    sandbox = sandbox_from_env()
    log.info("analytics configured", sandbox=sandbox.name if sandbox else "none")
    analyst = Analyst(llm, models or ModelConfig.from_env(), limits_from_env(), sandbox)
    return AnalyticsService(analyst, max_datasets=int(os.getenv("ANALYTICS_MAX_DATASETS") or 8))

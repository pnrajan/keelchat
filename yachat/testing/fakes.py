"""Fake tool backends for tests, sims and the dev console. Model calls are never faked: see runtime/llm_factory.py."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from yachat.capabilities.tools import ToolError

JOBS = [
    {"id": "j1", "title": "Backend Engineer", "location": "Austin, TX 78701", "pay": "$150k", "description": "Python services."},
    {"id": "j2", "title": "Data Analyst", "location": "Remote", "pay": "$110k", "description": "SQL and dashboards."},
]
PASSAGES = [
    {"title": "Benefits", "text": "We offer health, dental and a 401k match.", "source": "hr/benefits"},
    {"title": "Remote work", "text": "Most roles are hybrid; some are fully remote.", "source": "hr/remote"},
    {"title": "Commute", "text": "The Austin office is 10 minutes from 78704.", "source": "hr/office"},
]
SKILLS = [{"name": "SQL", "level": "core", "related_roles": ["Data Analyst"]}, {"name": "Python", "level": "core", "related_roles": ["Data Analyst"]}]
ROLES = [
    {"title": "Data Analyst", "summary": "Turns data into decisions.", "typical_skills": ["SQL", "Python"]},
    {"title": "Support Lead", "summary": "Runs a support team.", "typical_skills": ["communication"]},
]


class FakeTools:
    """Fake implementations keyed by tool/action name, with knobs for delay and failure."""

    def __init__(
        self, jobs: list[dict[str, Any]] | None = None, passages: list[dict[str, Any]] | None = None, delay_s: float = 0.0, fail: set[str] | None = None
    ):
        self.jobs = jobs if jobs is not None else JOBS
        self.passages = passages if passages is not None else PASSAGES
        self.delay_s = delay_s
        self.fail = fail or set()
        self.calls: dict[str, list[dict[str, Any]]] = {
            k: [] for k in ("analytics_query", "job_search", "knowledge_search", "submit_application", "skills_search", "role_search", "handoff_webhook")
        }
        self.submitted: list[dict[str, Any]] = []

    async def _gate(self, name: str, args: dict[str, Any]) -> None:
        self.calls[name].append(args)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if name in self.fail:
            raise ToolError(name, "upstream", f"{name} unavailable")

    async def job_search(self, role: str, location: str = "") -> list[dict[str, Any]]:
        await self._gate("job_search", {"role": role, "location": location})
        return list(self.jobs)

    async def knowledge_search(self, query: str, dataset: Any = None) -> list[dict[str, Any]]:
        await self._gate("knowledge_search", {"query": query})
        q = query.lower()
        hits = [p for p in self.passages if any(w in (p["text"] + p["title"]).lower() for w in q.split() if len(w) > 3)]
        return hits or self.passages[:1]

    async def submit_application(self, job_id: str, answers: dict[str, str]) -> dict[str, Any]:
        await self._gate("submit_application", {"job_id": job_id, "answers": answers})
        self.submitted.append({"job_id": job_id, "answers": answers})
        return {"application_id": f"app_{len(self.submitted)}", "job_id": job_id}

    async def handoff_webhook(
        self,
        reason: str,
        session_id: str,
        agent: str,
        fields: dict[str, str],
        transcript: list[dict[str, str]],
        ticket_id: str | None = None,
        callback_url: str | None = None,
    ) -> dict[str, Any]:
        args = {"reason": reason, "session_id": session_id, "agent": agent, "fields": fields, "transcript": transcript, "ticket_id": ticket_id}
        if callback_url:
            args["callback_url"] = callback_url
        await self._gate("handoff_webhook", args)
        return {"ticket_id": ticket_id or f"hof_{len(self.calls['handoff_webhook'])}"}

    async def skills_search(self, query: str) -> list[dict[str, Any]]:
        await self._gate("skills_search", {"query": query})
        return list(SKILLS)

    async def role_search(self, query: str) -> list[dict[str, Any]]:
        await self._gate("role_search", {"query": query})
        return list(ROLES)

    async def analytics_query(self, question: str, dataset: Any, history: Any = None, repairs: Any = None) -> dict[str, Any]:
        """A minimal AnalysisResult; the Analyst itself is exercised against the real model in tests/test_analytics.py."""
        await self._gate("analytics_query", {"question": question})
        from yachat.analytics.models import AnalysisResult  # the real model, so the fake's shape cannot drift

        name = getattr(dataset, "name", str(dataset))
        return AnalysisResult.model_validate(
            {
                "status": "ok",
                "input": {"question": question, "resolved_question": question, "dataset": name, "dataset_version": "fake"},
                "data": {"sql": "SELECT 1", "columns": [], "rows": [], "row_count": 0, "truncated": False},
                "assumptions": [],
                "outcome": {"method": "sql", "findings": [], "statistics": {}, "limitations": [], "attempts": {"sql": 1}},
                "summary": "Fake analysis.",
                "recommendations": [],
            }
        ).model_dump(mode="json")

    def impls(self) -> dict[str, Callable[..., Any]]:
        return {
            "analytics_query": self.analytics_query,
            "job_search": self.job_search,
            "knowledge_search": self.knowledge_search,
            "submit_application": self.submit_application,
            "skills_search": self.skills_search,
            "role_search": self.role_search,
            "handoff_webhook": self.handoff_webhook,
        }

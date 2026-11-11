"""Built-in tools. Real implementations are thin HTTP clients; services own their sources.

Tests substitute fake implementations by name (Runtime(tool_impls=...)); the registry metadata stays.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from pydantic import BaseModel, TypeAdapter

from keelchat.datasets.models import Dataset
from keelchat.registry.core import action, tool
from keelchat.runtime import usage

_CALLS = TypeAdapter(list[usage.Usage])


class ToolError(Exception):
    def __init__(self, name: str, kind: str, message: str):
        self.tool, self.kind = name, kind
        super().__init__(f"{name}: {kind}: {message}")


class Job(BaseModel):
    id: str
    title: str
    location: str
    pay: str | None = None
    description: str = ""


class Passage(BaseModel):
    title: str
    text: str
    source: str = ""


class Receipt(BaseModel):
    application_id: str
    job_id: str


async def post_json(name: str, url: str, payload: dict[str, Any], timeout_s: float, headers: dict[str, str] | None = None) -> Any:
    """POST JSON to a tool backend; any transport, status or decoding failure is a structured `upstream` ToolError."""
    try:
        async with httpx.AsyncClient(timeout=timeout_s, headers=headers or {}) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            return r.json()
    except Exception as e:  # transport, status, decoding or a blocked socket: structured; vendor text is never shown
        raise ToolError(name, "upstream", e.__class__.__name__) from e


async def _post(name: str, env_var: str, path: str, payload: dict[str, Any], timeout_s: float) -> Any:
    base = os.getenv(env_var)
    if not base:
        raise ToolError(name, "config", f"{env_var} is not configured")
    headers = {"authorization": f"Bearer {k}"} if (k := os.getenv("KEELCHAT_API_KEY")) else {}
    return await post_json(name, base.rstrip("/") + path, payload, timeout_s, headers)


@tool("job_search", translate=False, timeout_s=5.0)
async def job_search(role: str, location: str = "") -> list[Job]:
    """Search open jobs. Company rule: job text is shown as posted (never translated)."""
    data = await _post("job_search", "JOB_SEARCH_URL", "/search", {"role": role, "location": location}, 5.0)
    return [Job.model_validate(j) for j in data.get("jobs", [])]


@tool("knowledge_search", timeout_s=30.0, dataset="unstructured")
async def knowledge_search(query: str, dataset: Dataset | None = None) -> list[Passage]:
    """Search the knowledge base, or one unstructured dataset by name. Thin client to the Knowledge Service.
    The timeout covers indexing a dataset version the service has not seen yet."""
    payload: dict[str, Any] = {"query": query, **({"dataset": dataset.model_dump(mode="json")} if dataset else {})}
    data = await _post("knowledge_search", "KNOWLEDGE_URL", "/search", payload, 30.0)
    return [Passage.model_validate(p) for p in data.get("results", [])]


#: repair attempts per stage: SQL rewrites, code rewrites, re-interpretations, re-dos after a failed verification
#: (default and maximum; policies may lower them)
ANALYTICS_REPAIRS = {"sql": 3, "code": 3, "interpret": 2, "verify": 2}


@tool("analytics_query", timeout_s=240.0, retries=0, dataset="structured", repairs=ANALYTICS_REPAIRS)
async def analytics_query(
    question: str, dataset: Dataset, history: list[dict[str, str]] | None = None, repairs: dict[str, int] | None = None
) -> dict[str, Any]:
    """Answer a data question over a structured dataset: SQL, statistics when needed, findings and recommendations.
    Returns an AnalysisResult. Thin client to the Analytics Service (ANALYTICS_URL). Not retried: one analysis can make
    several model calls."""
    payload = {"question": question, "dataset": dataset.model_dump(mode="json"), "history": history or [], "repairs": repairs or {}}
    data: dict[str, Any] = await _post("analytics_query", "ANALYTICS_URL", "/analyze", payload, 240.0)
    usage.add(_CALLS.validate_python(data.pop("usage", [])))  # the service's model calls, on this turn's cost
    return data


@action("submit_application", by="candidate", timeout_s=10.0)
async def submit_application(job_id: str, answers: dict[str, str]) -> Receipt:
    """Submit an application. Runs only from the candidate's own submit action."""
    data = await _post("submit_application", "JOB_SEARCH_URL", "/apply", {"job_id": job_id, "answers": answers}, 10.0)
    return Receipt.model_validate(data)


class HandoffReceipt(BaseModel):
    ticket_id: str


@tool("handoff_webhook", timeout_s=10.0, retries=0, side_effects="write")
async def handoff_webhook(
    reason: str,
    session_id: str,
    agent: str,
    fields: dict[str, str],
    transcript: list[dict[str, str]],
    ticket_id: str | None = None,
    callback_url: str | None = None,
) -> HandoffReceipt:
    """Pass a conversation to a person: POST a ticket (or, with `ticket_id`, a follow-up message) to HANDOFF_URL, which
    returns {"ticket_id": ...}. HANDOFF_TOKEN, when set, is sent as a bearer token. A new ticket carries `callback_url`:
    POST {"status": "resolved"} there when done and the conversation resumes. Not retried: a retry could open a second
    ticket."""
    url = os.getenv("HANDOFF_URL")
    if not url:
        raise ToolError("handoff_webhook", "config", "HANDOFF_URL is not configured")
    headers = {"authorization": f"Bearer {k}"} if (k := os.getenv("HANDOFF_TOKEN")) else {}
    payload = {"reason": reason, "session_id": session_id, "agent": agent, "fields": fields, "transcript": transcript, "ticket_id": ticket_id}
    if callback_url:
        payload["callback_url"] = callback_url
    return HandoffReceipt.model_validate(await post_json("handoff_webhook", url, payload, 10.0, headers))


class Skill(BaseModel):
    name: str
    level: str = ""
    related_roles: list[str] = []


class Role(BaseModel):
    title: str
    summary: str = ""
    typical_skills: list[str] = []


@tool("skills_search", timeout_s=5.0)
async def skills_search(query: str) -> list[Skill]:
    """Find skills related to a role or interest (career catalogue service)."""
    data = await _post("skills_search", "CAREER_URL", "/skills", {"query": query}, 5.0)
    return [Skill.model_validate(s) for s in data.get("skills", [])]


@tool("role_search", timeout_s=5.0)
async def role_search(query: str) -> list[Role]:
    """Find roles matching interests or skills (career catalogue service)."""
    data = await _post("role_search", "CAREER_URL", "/roles", {"query": query}, 5.0)
    return [Role.model_validate(r) for r in data.get("roles", [])]

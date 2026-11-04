"""FastAPI surface over the Runtime."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from starlette.routing import Route

from yachat.api.keys import ADMIN, ApiKeyStore, InMemoryApiKeyStore, Principal, Scope, new_key, resolve, scope_for
from yachat.api.public import PUBLIC_PREFIXES, EmbedIn, EmbedUpdate, channels, public_router, snippet
from yachat.capabilities.nodes.survey_answers import SurveyQuestion
from yachat.capabilities.tools import ToolError
from yachat.channels import CHANNELS
from yachat.channels.whatsapp_cloud import WhatsAppSender
from yachat.datasets.models import NAME_PATTERN, Dataset, DatasetIn
from yachat.datasets.sources import SourceRejected, check_sources
from yachat.logging import configure, get_logger
from yachat.policy.compiler import CompiledPolicy, build
from yachat.policy.drafter import draft_policy
from yachat.policy.errors import PolicyError, PolicyValidationError
from yachat.policy.schema import policy_json_schema
from yachat.policy.summary import plain_summary
from yachat.policy.templates import builtin_templates, renamed
from yachat.registry.contract import ContractViolation, check_tool
from yachat.registry.core import REGISTRY, RegistryError
from yachat.registry.tenant_tools import TenantToolIn, TenantToolRejected, check_public_url, tenant_tool_spec
from yachat.runtime.audit import RETENTION_DAYS, audit_db, dashboard, retention_cutoff, summarize, usage_report
from yachat.runtime.findings import csv_rows, findings
from yachat.runtime.llm import LLMError, ModelConfig, small_only
from yachat.runtime.llm_factory import fake_tools_from_env, llm_from_env
from yachat.runtime.readiness import readiness
from yachat.runtime.runtime import NotFound, PolicyRetired, Runtime, RuntimeFailure
from yachat.state.artifacts import ArtifactStore, artifact_store_from_env
from yachat.state.models import AuditEvent, AuditKind, Embed, Handoff, HandoffStatus, PolicyTemplate, PolicyVersion, SessionState, subject_of
from yachat.state.store import (
    EmbedStore,
    InMemoryEmbedStore,
    InMemoryJobStore,
    InMemoryTenantToolStore,
    InsightJob,
    JobStore,
    TenantToolStore,
    VersionConflict,
)
from yachat.voice import Speaker, Transcriber

log = get_logger("api")

#: the built test console (frontend/dist), optional
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


TenantId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
DatasetName = Annotated[str, PathParam(pattern=NAME_PATTERN)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentUpload(Body):
    yaml: str = Field(max_length=200_000)
    tenant: TenantId = "default"


class VersionIn(AgentUpload):
    note: str = Field(default="", max_length=500)
    #: make it the version new sessions start on
    activate: bool = True


AgentName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=63)]


class FromTemplateIn(Body):
    template: str
    agent: AgentName
    note: str = Field(default="", max_length=500)


class CopyIn(Body):
    agent: AgentName
    #: the version to copy; default the active one, else the newest
    version: int | None = None


class TemplateIn(Body):
    id: AgentName
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    #: the saved agent whose policy becomes the template (its active version, else the newest)
    agent: str


class DraftIn(Body):
    description: str = Field(min_length=10, max_length=4_000)
    tenant: TenantId = "default"


class SessionCreate(Body):
    agent: str
    user_id: str
    tenant: TenantId = "default"
    channel: str = "web"
    policy_hash: str | None = None


class HandoffUpdate(Body):
    """What a person does on a handoff: claim it (status claimed, with who), resolve it, add a note."""

    status: HandoffStatus | None = None
    assignee: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = Field(default=None, max_length=4_000)
    #: on `resolved`: give the conversation back to the assistant (default), or end it
    resume: bool = True
    #: on `resolved`: answers the person collected, by field name; the assistant will not ask for them again
    fields: dict[str, str] | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def _claimed_by_someone(self) -> HandoffUpdate:
        if self.status == "claimed" and not self.assignee:
            raise ValueError("claiming a handoff needs an assignee")
        return self


class HandoffCallback(HandoffUpdate):
    """What a helpdesk reports on its callback_url: `claimed` (optional), then `resolved` once."""

    status: Literal["claimed", "resolved"]  # narrower than the inbox's


class SimConversationIn(Body):
    """One simulated conversation: scripted `messages`, or a `persona` the model plays."""

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    messages: list[str] | None = Field(default=None, min_length=1, max_length=30)
    persona: str | None = Field(default=None, min_length=5, max_length=1_000)
    max_turns: int = Field(default=12, ge=1, le=20)
    channel: str = "web"

    @model_validator(mode="after")
    def _one_kind(self) -> SimConversationIn:
        if (self.messages is None) == (self.persona is None):
            raise ValueError("give either messages or a persona")
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}")
        return self


class SimulateIn(Body):
    conversations: list[SimConversationIn] = Field(min_length=1, max_length=10)


class MetricsQuestionIn(Body):
    question: str = Field(min_length=3, max_length=500)
    days: int = Field(default=30, ge=1, le=90)
    agent: str | None = None


class TurnIn(Body):
    text: str | None = Field(default=None, max_length=8_000)
    channel_message_id: str | None = None
    #: a candidate-only action taken in the UI (e.g. submit_application)
    action: str | None = None
    payload: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


async def require_api_key(request: Request) -> None:
    """A key is required as `Authorization: Bearer <key>` on every route (health excepted) once YACHAT_API_KEY or
    YACHAT_TENANT_KEYS is set (yachat/api/keys.py). The key's scopes must cover the request, and a tenant
    key reaches only its own tenant: any other tenant in the path or query is refused (403); bodies are checked by
    `own()` in the handlers."""
    if request.url.path in ("/", "/health") or request.url.path.startswith(PUBLIC_PREFIXES):
        return  # liveness and the published chat stay open (public routes check their own token); every other route needs a key
    who = await resolve(request.headers.get("authorization"), request.app.state.keys)
    if who is None:
        raise HTTPException(401, "missing or invalid API key")
    request.state.principal = who
    need = scope_for(request.method, request.url.path)
    if need is not None and not who.may(need):
        raise HTTPException(403, f"this key cannot {need} (scopes: {', '.join(sorted(who.scopes))})")
    for asked in (request.path_params.get("tenant"), request.query_params.get("tenant")):
        if not who.admin and asked is not None and asked != who.tenant:
            raise HTTPException(403, f"this key is for tenant {who.tenant!r}")


def caller(request: Request) -> Principal:
    p: Principal = getattr(request.state, "principal", Principal(ADMIN))
    return p


def own(request: Request, tenant: str) -> str:
    """The tenant a body or an optional parameter names, for this caller: a tenant key's own tenant when it leaves the
    default, a 403 when it names another."""
    who = caller(request)
    if who.admin:
        return tenant
    if tenant not in ("default", who.tenant):
        raise HTTPException(403, f"this key is for tenant {who.tenant!r}")
    return who.tenant


def mcp_security() -> Any:
    """Every /mcp request carries a key, so Host checks (DNS-rebinding protection for local servers) are only on when
    YACHAT_MCP_ALLOWED_HOSTS lists the hosts to accept."""
    from mcp.server.transport_security import TransportSecuritySettings

    hosts = [h.strip() for h in (os.getenv("YACHAT_MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    return TransportSecuritySettings(enable_dns_rebinding_protection=bool(hosts), allowed_hosts=hosts, allowed_origins=[f"https://{h}" for h in hosts])


def stores_from_env() -> dict[str, Any]:
    """REDIS_URL → Redis sessions (7-day TTL), else DATABASE_URL → Postgres sessions; DATABASE_URL → Postgres memory, dataset catalog, saved policy
    versions, audit log, handoffs, spending limits, rate limits and per-conversation locks; in-memory otherwise."""
    out: dict[str, Any] = {}
    if url := os.getenv("REDIS_URL"):
        from yachat.state.redis_store import RedisSessionStore

        out["sessions"] = RedisSessionStore.from_url(url)
    if url := os.getenv("DATABASE_URL"):
        from yachat.state.postgres_store import (
            PostgresAgentVersionStore,
            PostgresAuditStore,
            PostgresBudgetStore,
            PostgresDatasetStore,
            PostgresHandoffStore,
            PostgresMemoryStore,
            PostgresRateLimiter,
            PostgresResponseStore,
            PostgresSessionStore,
            PostgresTemplateStore,
            PostgresTurnLocks,
        )

        if "sessions" not in out:  # no Redis: conversations live in Postgres, so they survive restarts
            out["sessions"] = PostgresSessionStore(url=url)

        out["memory"] = PostgresMemoryStore(url=url)
        out["audit"] = PostgresAuditStore(url=url)
        out["handoffs"] = PostgresHandoffStore(url=url)
        out["responses"] = PostgresResponseStore(url=url)
        out["datasets"] = PostgresDatasetStore(url=url)
        out["versions"] = PostgresAgentVersionStore(url=url)
        out["templates"] = PostgresTemplateStore(url=url)
        out["budgets"] = PostgresBudgetStore(url=url)
        out["limits"] = PostgresRateLimiter(url=url)  # rate limits and per-conversation locks shared by every server
        out["locks"] = PostgresTurnLocks(url=url)
    from yachat.state.mem0_store import mem0_from_env

    if (mem0 := mem0_from_env()) is not None:  # MEM0_API_KEY or MEM0_QDRANT_URL / _PATH: user memory in mem0
        out["memory"] = mem0
    from yachat.state.store import ArchivedAuditStore, InMemoryAuditStore

    if os.getenv("AUDIT_ARCHIVE_WAREHOUSE"):
        from yachat.state.iceberg_store import archive_from_env

        archive = archive_from_env()
        assert archive is not None
        out["audit"] = ArchivedAuditStore(out.get("audit") or InMemoryAuditStore(), archive, hot_days=int(os.getenv("AUDIT_HOT_DAYS") or 7))
    if out:
        log.info("external stores", stores=sorted(out))
    return out


#: the service that reads each kind of dataset; registration asks it to load the sources, so bad data fails early
DATASET_SERVICES = {"unstructured": "KNOWLEDGE_URL", "structured": "ANALYTICS_URL"}


async def warm_dataset(ds: Dataset) -> None:
    """Ask the data service to load the dataset. Its 422 reason (a rejected source) is passed on to the caller."""
    import httpx

    base = os.environ[DATASET_SERVICES[ds.kind]]
    headers = {"authorization": f"Bearer {k}"} if (k := os.getenv("YACHAT_API_KEY")) else {}
    try:
        async with httpx.AsyncClient(timeout=120.0, headers=headers) as client:
            r = await client.post(base.rstrip("/") + "/datasets", json=ds.model_dump(mode="json"))
    except httpx.HTTPError as e:
        raise ToolError("dataset", "upstream", e.__class__.__name__) from e
    if r.status_code == 422:
        detail = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
        raise SourceRejected(detail if isinstance(detail, str) else "the data service rejected the sources")
    if r.status_code >= 400:
        raise ToolError("dataset", "upstream", f"status {r.status_code}")


async def purge_dataset(ds: Dataset) -> None:
    """Remove an unstructured dataset's documents and vectors from the Knowledge Service."""
    import httpx

    base = os.environ["KNOWLEDGE_URL"]
    headers = {"authorization": f"Bearer {k}"} if (k := os.getenv("YACHAT_API_KEY")) else {}
    try:
        async with httpx.AsyncClient(timeout=120.0, headers=headers) as client:
            r = await client.delete(base.rstrip("/") + f"/datasets/{ds.tenant}/{ds.name}", params={"version": ds.version})
    except httpx.HTTPError as e:
        raise ToolError("dataset", "upstream", e.__class__.__name__) from e
    if r.status_code >= 400:
        raise ToolError("dataset", "upstream", f"status {r.status_code}")


def _tool_view(spec: Any) -> dict[str, Any]:
    """Tenant tool as returned by the API: never the headers it was registered with."""
    return {
        "name": spec.name,
        "tenant": spec.tenant,
        "description": spec.description,
        "url": spec.url,
        "input_schema": spec.input_schema,
        "output_schema": spec.output_schema,
        "timeout_s": spec.timeout_s,
        "retries": spec.retries,
    }


def tool_store_from_env() -> TenantToolStore:
    """DATABASE_URL → Postgres (durable, shared by replicas); in-memory otherwise."""
    if url := os.getenv("DATABASE_URL"):
        from yachat.state.postgres_store import PostgresTenantToolStore

        return PostgresTenantToolStore(url=url)
    return InMemoryTenantToolStore()


async def sync_tenant_tools(store: TenantToolStore, tenant: str | None = None) -> int:
    """Load stored registrations this replica has not seen yet into the registry (all tenants when `tenant` is None)."""
    added = 0
    for t, registration in await store.list(tenant):
        body = TenantToolIn.model_validate(registration)
        if not REGISTRY.has_tool(body.name, t):
            REGISTRY.add_tool(tenant_tool_spec(t, body))
            added += 1
    return added


def key_store_from_env() -> ApiKeyStore:
    """DATABASE_URL → issued API keys in Postgres (hashes only); in-memory otherwise."""
    if url := os.getenv("DATABASE_URL"):
        from yachat.state.postgres_store import PostgresApiKeyStore

        return PostgresApiKeyStore(url=url)
    return InMemoryApiKeyStore()


def job_store_from_env() -> JobStore:
    """DATABASE_URL → insight jobs in Postgres (results outlive the machine); in-memory otherwise."""
    if url := os.getenv("DATABASE_URL"):
        from yachat.state.postgres_store import PostgresJobStore

        return PostgresJobStore(url=url)
    return InMemoryJobStore()


def public_base(request: Request) -> str:
    """Where personal links point: YACHAT_PUBLIC_URL, else the address this request came in on."""
    return (os.getenv("YACHAT_PUBLIC_URL") or str(request.base_url)).rstrip("/")


#: responses an aggregates request reads at most
AGGREGATE_LIMIT = 50_000


class RefineIn(Body):
    yaml: str = Field(min_length=10, max_length=50_000)
    #: plays at most (the first, then revisions); each is a few minutes and costs model calls
    rounds: int = Field(default=2, ge=1, le=3)


class WhatsAppIn(Body):
    phone_number_id: str = Field(pattern=r"^[0-9]{5,30}$")
    access_token: str = Field(min_length=20, max_length=1_000)
    app_secret: str = Field(min_length=16, max_length=200)


AssistantKind = Literal["answers", "support", "survey", "custom"]
#: the template each starting type drafts from
ASSISTANT_TEMPLATES: dict[str, str] = {"answers": "blank", "support": "support", "survey": "survey", "custom": "blank"}


class PublishIn(BaseModel):
    name: str = Field(min_length=2, max_length=60)
    yaml: str = Field(min_length=10, max_length=50_000)
    greeting: str = Field(default="", max_length=300)
    allowed_origins: list[str] = Field(default_factory=list, max_length=20)


class InstallCheckIn(Body):
    url: str = Field(min_length=8, max_length=500)


def embed_store_from_env() -> EmbedStore:
    """DATABASE_URL → published agents (widget, app) in Postgres; in-memory otherwise."""
    if url := os.getenv("DATABASE_URL"):
        from yachat.state.postgres_store import PostgresEmbedStore

        return PostgresEmbedStore(url=url)
    return InMemoryEmbedStore()


#: the longest a workspace (non-admin) key may live; a leaked key stops working by then even if nobody revokes it
MAX_TENANT_KEY_DAYS = 365


class InsightsIn(Body):
    """Which chats to read: agents (empty: every agent in the workspace) over the last `days`."""

    agents: list[str] = Field(default_factory=list, max_length=50)
    days: int = Field(default=30, ge=1, le=365)
    #: a plain-English request ("why were people unhappy with support last month?"); it picks the agents, the window
    #: and the focus, and the report answers it. Overrides `agents` and `days`.
    question: str | None = Field(default=None, max_length=500)


class PlanIn(Body):
    """A workspace's plan. None: no limit of that kind."""

    limit_turns: int | None = Field(default=None, ge=0)
    limit_usd: float | None = Field(default=None, ge=0)
    small_models: bool = False
    paid: bool = False


class KeyIn(Body):
    name: str = Field(min_length=1, max_length=80)
    scopes: list[Scope] = Field(min_length=1)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


def create_app(
    runtime: Runtime | None = None,
    tool_store: TenantToolStore | None = None,
    keys: ApiKeyStore | None = None,
    embeds: EmbedStore | None = None,
    voice: tuple[Transcriber, Speaker] | None = None,
    whatsapp: WhatsAppSender | None = None,
    jobs: JobStore | None = None,
    artifacts: ArtifactStore | None = None,
) -> FastAPI:
    load_dotenv()
    configure(logging.INFO)
    from yachat.tracing import init as init_tracing

    init_tracing("api")
    if not os.getenv("YACHAT_API_KEY"):
        log.warning("YACHAT_API_KEY is not set: the API is open to anyone who can reach it")
    tools = fake_tools_from_env()
    if tools:
        log.warning("FakeTools in use for tools without a configured backend", faked=sorted(tools))
    artifacts = artifacts or artifact_store_from_env()  # uploaded content and slide decks
    rt = runtime or Runtime(llm_from_env(), tools, models=ModelConfig.from_env(), **stores_from_env())
    tool_store = tool_store or tool_store_from_env()

    from yachat.api.mcp_server import RequireKey, build_mcp

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if n := await sync_tenant_tools(tool_store):
            log.info("tenant tools loaded", tools=n)
        if n := await rt.load_saved_policies():
            log.info("saved policies loaded", agents=n)
        tasks = [
            asyncio.create_task(purge_audit()),
            asyncio.create_task(refresh_prices()),
            asyncio.create_task(export_audit()),
        ]
        try:
            async with mcp_server.session_manager.run():  # the /mcp endpoint's sessions
                yield
        finally:
            for task in tasks:
                task.cancel()

    async def export_audit() -> None:
        """With an archive (AUDIT_ARCHIVE_WAREHOUSE): settled audit records are copied to it hourly."""
        from yachat.state.store import ArchiveConflict, ArchivedAuditStore

        if not isinstance(rt.audit, ArchivedAuditStore):
            return
        days = int(os.getenv("AUDIT_RETENTION_DAYS") or RETENTION_DAYS)
        while True:
            try:
                if n := await rt.audit.export(first=retention_cutoff(days)):
                    log.info("audit records archived", records=n)
            except ArchiveConflict:  # another replica exported first; next run continues from its cursor
                log.info("audit export skipped: another exporter committed first")
            except Exception as e:  # retried next hour; never stops the server
                log.error("audit export failed", error=f"{e.__class__.__name__}: {e}")
            await asyncio.sleep(3_600)

    async def refresh_prices() -> None:
        """Model prices from OpenRouter at startup and daily (YACHAT_OPENROUTER_PRICES=off to skip)."""
        from yachat.runtime.usage import REFRESH_S, refresh_openrouter

        if (os.getenv("YACHAT_OPENROUTER_PRICES") or "on").lower() in ("off", "0", "false"):
            return
        while True:
            await refresh_openrouter()
            await asyncio.sleep(REFRESH_S)

    async def purge_audit() -> None:
        """Audit records older than AUDIT_RETENTION_DAYS (default 90) are deleted at startup and daily."""
        days = int(os.getenv("AUDIT_RETENTION_DAYS") or RETENTION_DAYS)
        while True:
            try:
                if n := await rt.audit.purge(retention_cutoff(days)):
                    log.info("audit records purged", records=n, retention_days=days)
                if (purge := getattr(rt.sessions, "purge", None)) is not None and (n := await purge()):
                    log.info("expired sessions purged", sessions=n)
                for store in (rt.limits, rt.locks):  # expired rate-limit hits and lock leases
                    if (purge := getattr(store, "purge", None)) is not None:
                        await purge()
            except Exception as e:  # retried tomorrow; never stops the server
                log.error("audit purge failed", error=f"{e.__class__.__name__}: {e}")
            await asyncio.sleep(86_400)

    app = FastAPI(title="yachat", version="0.2.0", dependencies=[Depends(require_api_key)], lifespan=lifespan)
    app.state.runtime = rt
    mcp_server = build_mcp(app)
    # /mcp: Claude and other MCP clients build agents for the key's tenant (yachat/api/mcp_server.py)
    mcp_asgi = RequireKey(mcp_server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, transport_security=mcp_security()), app)
    app.router.routes.append(Route("/mcp", mcp_asgi, methods=["GET", "POST", "DELETE"]))  # an ASGI app, before the console mount
    app.state.tool_store = tool_store
    app.state.keys = keys or key_store_from_env()
    embed_store = embeds or embed_store_from_env()
    from yachat.voice import voice_from_env

    app.include_router(public_router(rt, embed_store, voice or voice_from_env(), whatsapp))
    from yachat.api.security import SecurityHeaders

    skip = (*PUBLIC_PREFIXES, "/docs", "/redoc")  # chat pages set their own framing rules; the API docs load their own assets
    app.add_middleware(SecurityHeaders, skip=skip)

    @app.post("/v1/tenants/{tenant}/keys", status_code=201)
    async def issue_key(tenant: TenantId, body: KeyIn, request: Request) -> dict[str, Any]:
        """Issue an API key for this tenant. The key is in this response only; afterwards only its id is shown. A
        tenant key may issue keys with scopes it has itself; the admin key may issue any."""
        who = caller(request)
        if not all(who.may(sc) for sc in body.scopes):
            raise HTTPException(403, "a key cannot issue scopes it does not have")
        if not (os.getenv("YACHAT_API_KEY") or os.getenv("YACHAT_TENANT_KEYS")):
            raise HTTPException(409, "set YACHAT_API_KEY first: issued keys mean nothing on an open server")
        if not who.admin and not (body.expires_in_days and body.expires_in_days <= MAX_TENANT_KEY_DAYS):
            raise HTTPException(422, f"a workspace key must expire within {MAX_TENANT_KEY_DAYS} days (expires_in_days)")
        raw, rec = new_key(tenant, body.name, list(body.scopes), body.expires_in_days)
        await app.state.keys.add(rec)
        log.info("api key issued", tenant=tenant, key=rec.id, scopes=rec.scopes, by=who.key_id or "admin")
        return rec.view() | {"key": raw}

    @app.get("/v1/tenants/{tenant}/keys")
    async def list_keys(tenant: TenantId) -> list[dict[str, Any]]:
        return [k.view() for k in await app.state.keys.list(tenant)]

    @app.delete("/v1/tenants/{tenant}/keys/{key_id}")
    async def revoke_key(tenant: TenantId, key_id: str, request: Request) -> dict[str, str]:
        """Revoke a key now: its next request is refused."""
        if not await app.state.keys.revoke(tenant, key_id):
            raise NotFound(f"active key {key_id!r}")
        log.info("api key revoked", tenant=tenant, key=key_id, by=caller(request).key_id or "admin")
        return {"revoked": key_id}

    @app.exception_handler(NotFound)
    async def _nf(_: Request, e: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(PolicyRetired)
    async def _pr(_: Request, e: PolicyRetired) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(VersionConflict)
    async def _vc(_: Request, e: VersionConflict) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=409)

    @app.exception_handler(RuntimeFailure)
    async def _rf(_: Request, e: RuntimeFailure) -> JSONResponse:
        return JSONResponse({"detail": str(e)}, status_code=400)

    async def compile_upload(body: AgentUpload) -> CompiledPolicy:
        await sync_tenant_tools(tool_store, body.tenant)  # a tool registered on another replica
        catalog = {d.name: d.kind for d in await rt.datasets.list(body.tenant)}
        try:
            return build(body.yaml, tenant=body.tenant, datasets=catalog)
        except PolicyValidationError as e:
            raise HTTPException(422, detail=[{"path": x.path, "message": x.message} for x in e.errors]) from None

    async def version_view(v: PolicyVersion, yaml: bool = False) -> dict[str, Any]:
        active = await rt.versions.active(v.tenant, v.agent)
        out = v.model_dump(mode="json", exclude=set() if yaml else {"yaml"})
        return out | {"active": active is not None and active.version == v.version}

    @app.post("/v1/agents", status_code=201)
    @app.post("/build", status_code=201)
    async def upload_agent(body: AgentUpload, request: Request) -> dict[str, Any]:
        """Build and register a policy to try it (new sessions of the agent start on it). Not saved: see versions."""
        compiled = await compile_upload(body.model_copy(update={"tenant": own(request, body.tenant)}))
        await rt.register_policy(compiled)
        return {
            "agent": compiled.agent,
            "tenant": compiled.tenant,
            "hash": compiled.hash,
            "mode": compiled.mode,
            "models": compiled.models,
            "allowed_tools": compiled.allowed_tools,
            "datasets": compiled.datasets,
        }

    @app.post("/v1/agents/validate")
    async def validate_agent(body: AgentUpload, request: Request) -> dict[str, Any]:
        """Every error the build would report, as YAML paths, without registering anything (the editor's live check)."""
        scoped = body.model_copy(update={"tenant": own(request, body.tenant)})  # a 403 is not a validation error
        try:
            compiled = await compile_upload(scoped)
        except HTTPException as e:
            return {"errors": e.detail, "agent": None}
        return {"errors": [], "agent": compiled.agent}

    @app.post("/v1/tenants/{tenant}/agents/readiness")
    async def agent_readiness(tenant: TenantId, body: AgentUpload) -> dict[str, Any]:
        """What this policy still needs before launch: build errors, unconnected tools, consent, scope, safety checks,
        a saved version, tests and a channel. Computed in code; nothing is registered."""
        await sync_tenant_tools(tool_store, tenant)
        catalog = {d.name: d.kind for d in await rt.datasets.list(tenant)}
        report = await readiness(body.yaml, tenant, catalog, set(rt.tool_impls), rt.versions)
        return report.model_dump(mode="json")

    @app.post("/v1/agents/draft")
    async def draft_agent(body: DraftIn, request: Request) -> dict[str, Any]:
        """A first policy written by the model from a plain description, checked by the build and repaired from its
        errors. The result may still have errors; it is never registered or saved."""
        tenant = own(request, body.tenant)
        await sync_tenant_tools(tool_store, tenant)
        catalog = {d.name: d.kind for d in await rt.datasets.list(tenant)}
        async with spending(tenant) as models:
            draft = await draft_policy(rt.llm, models, body.description, tenant, catalog)
        return {"yaml": draft.yaml, "errors": [{"path": e.path, "message": e.message} for e in draft.errors], "attempts": draft.attempts}

    @app.post("/v1/tenants/{tenant}/agents", status_code=201)
    async def save_version(tenant: TenantId, body: VersionIn) -> dict[str, Any]:
        """Save a valid build as the agent's next version (named by the YAML's `agent`). Saving an unchanged policy
        returns its existing version. Invalid policies are rejected with their YAML paths and nothing is stored."""
        compiled = await compile_upload(body.model_copy(update={"tenant": tenant}))
        v, created = await rt.save_version(compiled, body.yaml, body.note, body.activate)
        return await version_view(v) | {"created": created}

    async def templates_for(tenant: str) -> list[PolicyTemplate]:
        return [*builtin_templates(), *await rt.templates.list(tenant)]

    async def current(tenant: str, agent: str, version: int | None = None) -> PolicyVersion:
        v = await (rt.versions.get(tenant, agent, version) if version is not None else rt.versions.active(tenant, agent))
        if v is None and version is None:
            v = next(iter(await rt.versions.versions(tenant, agent)), None)
        if v is None:
            raise NotFound(f"agent {agent!r}" + (f" version {version}" if version is not None else "") + " has no saved version")
        return v

    async def create_agent(tenant: str, agent: str, yaml: str, note: str) -> dict[str, Any]:
        """A new agent's v1 from a starting policy; never overwrites an agent that already has versions."""
        if await rt.versions.versions(tenant, agent):
            raise HTTPException(409, f"agent {agent!r} already exists; pick another name")
        compiled = await compile_upload(AgentUpload(yaml=renamed(yaml, agent), tenant=tenant))
        v, _ = await rt.save_version(compiled, renamed(yaml, agent), note)
        return await version_view(v)

    @app.get("/v1/tenants/{tenant}/templates")
    async def list_templates(tenant: TenantId) -> list[dict[str, Any]]:
        """Bot types to start a new agent from: the built-in ones, then this tenant's own."""
        return [t.model_dump(mode="json") for t in await templates_for(tenant)]

    @app.post("/v1/tenants/{tenant}/templates", status_code=201)
    async def save_template(tenant: TenantId, body: TemplateIn) -> dict[str, Any]:
        """Save one of the tenant's agents as a bot type (its active version, or the newest)."""
        if any(t.id == body.id for t in builtin_templates()):
            raise HTTPException(409, f"{body.id!r} is a built-in bot type; pick another id")
        v = await current(tenant, body.agent)
        t = await rt.templates.save(tenant, body.id, body.name, body.description, v.yaml)
        return t.model_dump(mode="json")

    @app.delete("/v1/tenants/{tenant}/templates/{template_id}")
    async def delete_template(tenant: TenantId, template_id: str) -> dict[str, str]:
        if any(t.id == template_id for t in builtin_templates()):
            raise HTTPException(409, "built-in bot types cannot be deleted")
        if not await rt.templates.delete(tenant, template_id):
            raise NotFound(f"template {template_id!r}")
        return {"deleted": template_id}

    @app.post("/v1/tenants/{tenant}/agents/from-template", status_code=201)
    async def agent_from_template(tenant: TenantId, body: FromTemplateIn) -> dict[str, Any]:
        """A new agent from a bot type, saved as its v1 and active. The policy must build for this tenant (an analyst
        template needs the dataset it names)."""
        t = next((x for x in await templates_for(tenant) if x.id == body.template), None)
        if t is None:
            raise NotFound(f"template {body.template!r}")
        return await create_agent(tenant, body.agent, t.yaml, body.note or f"from the {t.name} template")

    @app.post("/v1/tenants/{tenant}/agents/{agent}/copy", status_code=201)
    async def copy_agent(tenant: TenantId, agent: str, body: CopyIn) -> dict[str, Any]:
        """A new agent with another agent's policy (the active version, or the one named), saved as its v1."""
        v = await current(tenant, agent, body.version)
        return await create_agent(tenant, body.agent, v.yaml, f"copied from {agent} v{v.version}")

    @app.get("/v1/tenants/{tenant}/agents")
    async def list_agents(tenant: TenantId) -> list[dict[str, Any]]:
        return [a.model_dump(mode="json") for a in await rt.versions.agents(tenant)]

    @app.get("/v1/tenants/{tenant}/agents/{agent}/versions")
    async def list_versions(tenant: TenantId, agent: str) -> list[dict[str, Any]]:
        return [await version_view(v) for v in await rt.versions.versions(tenant, agent)]

    @app.get("/v1/tenants/{tenant}/agents/{agent}/versions/{version}")
    async def get_version(tenant: TenantId, agent: str, version: int) -> dict[str, Any]:
        v = await rt.versions.get(tenant, agent, version)
        if v is None:
            raise NotFound(f"version {version} of agent {agent!r}")
        return await version_view(v, yaml=True)

    @app.post("/v1/tenants/{tenant}/agents/{agent}/versions/{version}/activate")
    async def activate_version(tenant: TenantId, agent: str, version: int) -> dict[str, Any]:
        """New sessions start on this version; running sessions keep theirs. Activating an older version rolls back."""
        try:
            v = await rt.activate_version(tenant, agent, version)
        except PolicyValidationError as e:
            raise HTTPException(422, detail=[{"path": x.path, "message": x.message} for x in e.errors]) from None
        log.info("policy version activated", tenant=tenant, agent=agent, version=version)
        return await version_view(v)

    @app.get("/v1/policies/{tenant}/{agent}/{policy_hash}")
    async def get_policy(tenant: str, agent: str, policy_hash: str) -> dict[str, Any]:
        p = await rt.policies.get(policy_hash)
        if p is None or p.tenant != tenant or p.agent != agent:
            raise NotFound(f"policy {policy_hash} for {tenant}/{agent}")
        return p.model_dump(mode="json")

    @app.post("/v1/tenants/{tenant}/tools", status_code=201)
    @app.post("/tenants/{tenant}/tools", status_code=201)
    async def register_tenant_tool(tenant: TenantId, body: TenantToolIn) -> dict[str, Any]:
        """Tool Registry API: an HTTP endpoint becomes a tool visible only to this tenant's agents."""
        await sync_tenant_tools(tool_store, tenant)
        if REGISTRY.has_tool(body.name, tenant):  # cheap early answer; the store's key is the real arbiter
            raise HTTPException(409, f"{body.name!r} is already a tool for {tenant}")
        try:
            await asyncio.to_thread(check_public_url, body.url)  # DNS lookup off the event loop
            spec = tenant_tool_spec(tenant, body)
            await check_tool(spec)  # same contract as built-in tools: schemas, structured failure, timeout honoured
        except (TenantToolRejected, ContractViolation, ValueError) as e:
            raise HTTPException(422, str(e)) from None
        if not await tool_store.add(tenant, body.name, body.model_dump(mode="json")):  # a concurrent registration won
            raise HTTPException(409, f"{body.name!r} is already registered for {tenant}; register a new name to change it")
        try:
            REGISTRY.add_tool(spec)
        except RegistryError:  # another request on this replica loaded it from the store first: same registration
            pass
        log.info("tenant tool registered", tenant=tenant, tool=spec.name)
        return _tool_view(spec)

    @app.get("/v1/tenants/{tenant}/tools")
    @app.get("/tenants/{tenant}/tools")
    async def list_tenant_tools(tenant: TenantId) -> list[dict[str, Any]]:
        await sync_tenant_tools(tool_store, tenant)
        return [_tool_view(s) for s in REGISTRY.tenant_tools(tenant)]

    @app.put("/v1/tenants/{tenant}/datasets/{name}")
    async def put_dataset(tenant: TenantId, name: DatasetName, body: DatasetIn) -> dict[str, Any]:
        """Register or update a dataset. A new version replaces the sources; its kind cannot change (policies check it)."""
        prev = await rt.datasets.get(tenant, name)
        if prev is not None and prev.kind != body.spec.kind:
            raise HTTPException(409, f"{name!r} is a {prev.kind} dataset; register a new name for {body.spec.kind} data")
        ds = Dataset.create(tenant, name, body)
        try:
            if os.getenv(DATASET_SERVICES[ds.kind]):  # the service reads the sources, on its own disk: it checks them
                await warm_dataset(ds)
            else:
                await asyncio.to_thread(check_sources, body.spec)  # filesystem checks off the event loop
        except SourceRejected as e:
            raise HTTPException(422, str(e)) from None
        except ToolError as e:
            log.error("dataset could not be loaded", tenant=tenant, dataset=name, error=str(e))
            raise HTTPException(422, f"the {ds.kind} data service could not load this dataset") from None
        await rt.datasets.put(ds)
        log.info("dataset registered", tenant=tenant, dataset=name, kind=ds.kind, version=ds.version)
        return ds.model_dump(mode="json")

    @app.get("/v1/tenants/{tenant}/datasets")
    async def list_datasets(tenant: TenantId) -> list[dict[str, Any]]:
        return [d.model_dump(mode="json") for d in await rt.datasets.list(tenant)]

    @app.get("/v1/tenants/{tenant}/datasets/{name}")
    async def get_dataset(tenant: TenantId, name: DatasetName) -> dict[str, Any]:
        ds = await rt.datasets.get(tenant, name)
        if ds is None:
            raise NotFound(f"dataset {name!r}")
        return ds.model_dump(mode="json")

    @app.delete("/v1/tenants/{tenant}/datasets/{name}")
    async def delete_dataset(tenant: TenantId, name: DatasetName) -> dict[str, str]:
        """Delete a dataset. An unstructured one's documents and vectors are purged from the Knowledge Service first;
        if that fails the dataset is kept (502), so the delete can be retried and no data is left behind unlisted."""
        ds = await rt.datasets.get(tenant, name)
        if ds is None:
            raise NotFound(f"dataset {name!r}")
        if ds.kind == "unstructured" and os.getenv("KNOWLEDGE_URL"):
            try:
                await purge_dataset(ds)
            except ToolError as e:
                log.error("dataset purge failed", tenant=tenant, dataset=name, error=str(e))
                raise HTTPException(502, "the Knowledge Service could not remove this dataset's documents; nothing was deleted, try again") from None
        if not await rt.datasets.delete(tenant, name):
            raise NotFound(f"dataset {name!r}")
        log.info("dataset deleted", tenant=tenant, dataset=name)
        return {"deleted": name}

    @app.get("/v1/registry")
    @app.get("/registry")
    async def registry(tenant: TenantId | None = None) -> dict[str, Any]:
        """Platform tools, MCP tools and (with ?tenant=) that tenant's tools, plus nodes, guardrails, pre-processors."""
        if tenant is not None:
            await sync_tenant_tools(tool_store, tenant)
        manifest = REGISTRY.manifest(tenant).model_dump(mode="json")
        groups: dict[str, list[dict[str, Any]]] = {"platform": [], "mcp": [], "tenant": []}
        for e in manifest["entries"]:
            if e["kind"] == "tool":
                groups[e["properties"]["source"]].append(e)
        return {"tools": groups, "entries": [e for e in manifest["entries"] if e["kind"] != "tool"]}

    @app.get("/v1/whoami")
    async def whoami(request: Request) -> dict[str, Any]:
        """The tenant this key acts as. The admin key (or an open server) acts as `default` and may name any tenant."""
        who = caller(request)
        if who.admin:
            return {"tenant": "default", "admin": True}
        return {"tenant": who.tenant, "admin": False, "key": who.key_id, "scopes": sorted(who.scopes)}

    @app.get("/v1/schema")
    async def schema() -> dict[str, Any]:
        return policy_json_schema()

    @app.post("/v1/sessions", status_code=201)
    async def create_session(body: SessionCreate, request: Request) -> dict[str, Any]:
        tenant = own(request, body.tenant)
        await sync_tenant_tools(tool_store, tenant)  # its policy may use a tool registered on another replica
        s = await rt.start_session(body.agent, body.user_id, body.channel, body.policy_hash, tenant=tenant)
        return {"id": s.id, "agent": s.agent, "policy_hash": s.policy_hash, "channel": s.channel, "language": s.language}

    async def owned_session(request: Request, session_id: str) -> SessionState:
        st = await rt.session_state(session_id)
        who = caller(request)
        if not who.admin and who.tenant != st.tenant:
            raise HTTPException(403, "this session belongs to another tenant")
        return st

    @app.post("/v1/sessions/{session_id}/turns")
    async def turn(session_id: str, body: TurnIn, request: Request) -> dict[str, Any]:
        state = await owned_session(request, session_id)
        if body.payload is not None:
            return (await rt.turn_from_payload(session_id, body.payload)).to_dict()
        payload = CHANNELS[state.channel].payload_from_text(body.text or "", state.user_id, body.channel_message_id)
        if body.action:
            payload["action"] = body.action
        return (await rt.turn_from_payload(session_id, payload)).to_dict()

    @app.get("/v1/sessions/{session_id}/state")
    async def state(session_id: str, request: Request) -> dict[str, Any]:
        return (await owned_session(request, session_id)).model_dump(mode="json")

    @app.delete("/v1/users/{user_id}/memory")
    async def delete_memory(request: Request, user_id: str, agent: str | None = None, tenant: TenantId = "default") -> dict[str, Any]:
        """Delete what the platform keeps about a user: remembered items and, unless one agent is named, their survey answers."""
        t = own(request, tenant)
        out: dict[str, Any] = {"deleted": await rt.memory_writer.delete(user_id, agent, t)}
        out["audit_messages_cleared"] = await rt.audit.forget(t, subject_of(t, user_id))  # their words, not the counts
        if agent is None:
            out["survey_responses_deleted"] = await rt.responses.delete_user(t, user_id)
        return out

    @app.get("/v1/users/{user_id}/memory/export")
    async def export_memory(request: Request, user_id: str, agent: str, tenant: TenantId = "default") -> list[dict[str, Any]]:
        return await rt.memory_writer.export(user_id, agent, own(request, tenant))

    @app.post("/v1/tenants/{tenant}/agents/{agent}/simulate")
    async def simulate_agent(tenant: TenantId, agent: str, body: SimulateIn) -> dict[str, Any]:
        """Run simulated conversations against the agent's current policy (the last one built or activated), on fake
        tools and stores of their own: nothing is submitted, sent to a helpdesk, remembered or added to the audit log
        and inbox. Returns each conversation's transcript, outcome and decisions, and a summary."""
        from yachat.sim.harness import sim_runtime, simulate

        policy = await rt.policies.latest(agent, tenant)
        if policy is None:
            raise NotFound(f"no policy for agent {agent!r}; build or save one first")
        srt = sim_runtime(rt.llm, rt.models, datasets=rt.datasets, budgets=rt.budgets)  # simulated turns count too
        await srt.register_policy(policy)
        async with spending(tenant):  # and the model-played users (the simulated agent's turns follow the plan)
            report = await simulate(srt, policy, [c.model_dump(exclude_none=True) for c in body.conversations])
        log.info("simulation run", tenant=tenant, agent=agent, conversations=len(body.conversations), exit_reasons=report.exit_reasons)
        return report.to_dict() | {"policy_hash": policy.hash}

    @app.get("/v1/tenants/{tenant}/audit")
    async def audit_log(
        tenant: TenantId,
        agent: str | None = None,
        session_id: str | None = None,
        kind: Annotated[list[AuditKind] | None, Query()] = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> list[dict[str, Any]]:
        """The audit log, newest first: every decision the platform made (routes, accepted and rejected answers,
        counts, exits, handoffs, guardrail blocks, tool outcomes) and what people did on handoffs. Page back with
        `until` set to the oldest record's `at`."""
        events = await rt.audit.query(tenant, agent=agent, session_id=session_id, kinds=kind, since=since, until=until, limit=limit)
        return [e.model_dump(mode="json") for e in events]

    @asynccontextmanager
    async def audit_window(
        tenant: str, since: datetime, until: datetime | None = None, *, agent: str | None = None, kinds: list[AuditKind] | None = None
    ) -> AsyncIterator[Any]:
        """The tenant's records in [since, until) in a private DuckDB (`audit_db`), closed afterwards."""
        db = await audit_db(rt.audit, tenant, since, until, agent=agent, kinds=kinds)
        try:
            yield db
        finally:
            db.close()

    @app.get("/v1/tenants/{tenant}/audit/summary")
    async def audit_summary(tenant: TenantId, agent: str | None = None, since: datetime | None = None, until: datetime | None = None) -> dict[str, Any]:
        """Outcomes over a window: completion and handoff rates, why conversations go to a person, which questions
        fail, guardrail blocks and tool failures."""
        async with audit_window(tenant, since or datetime(1970, 1, 1, tzinfo=UTC), until, agent=agent) as db:  # no since: every record kept
            return (await asyncio.to_thread(summarize, db, since, until)).model_dump(mode="json")

    @app.get("/v1/tenants/{tenant}/dashboard")
    async def outcome_dashboard(tenant: TenantId, days: Annotated[int, Query(ge=1, le=90)] = 30, agent: str | None = None) -> dict[str, Any]:
        """This tenant's outcomes and service health per day and per agent: conversations completed, handed off and
        dropped, latency percentiles, model calls and errors per turn, and how fast people pick up handoffs."""
        until = datetime.now(UTC)
        since = (until - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        open_n = len(await rt.handoffs.list(tenant, status="open", agent=agent, limit=500))
        async with audit_window(tenant, since, agent=agent) as db:
            return (await asyncio.to_thread(dashboard, db, since, until, open_n)).model_dump(mode="json")

    @app.get("/v1/tenants/{tenant}/usage")
    async def usage(tenant: TenantId, days: Annotated[int, Query(ge=1, le=90)] = 30, agent: str | None = None) -> dict[str, Any]:
        """This tenant's model usage and cost: calls, input / output / cached tokens and USD, by model, purpose, agent
        and day, plus the models this server is configured with and where each price comes from."""
        from yachat.runtime.usage import price, prices_fetched_at

        until = datetime.now(UTC)
        since = (until - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        async with audit_window(tenant, since, agent=agent, kinds=["llm", "turn"]) as db:
            report = await asyncio.to_thread(usage_report, db, since, until)
        configured = {"generate": rt.models.generate, "classify": rt.models.classify, "verify": rt.models.verifier}
        used = {line.key for line in report.by_model} | set(configured.values())
        prices = {}
        for m in sorted(used):
            per_m_in, src = price(m, 1_000_000, 0)
            per_m_out, _ = price(m, 0, 1_000_000)
            prices[m] = {"source": src, "input_per_mtok": per_m_in, "output_per_mtok": per_m_out}
        fetched = prices_fetched_at()
        return report.model_dump(mode="json") | {
            "models": configured,
            "prices": prices,
            "openrouter_prices_at": datetime.fromtimestamp(fetched, UTC).isoformat() if fetched else None,
        }

    @app.get("/v1/tenants/{tenant}/usage/alternatives")
    async def cheaper_models(tenant: TenantId, days: Annotated[int, Query(ge=1, le=90)] = 30) -> dict[str, Any]:
        """Cheaper models for each job (writing replies, reading messages, checking answers, speech-to-text), priced on
        this workspace's own token use over the window. Only price is compared: run a simulation before switching."""
        from yachat.runtime.usage import cheaper, job_of, price, prices_fetched_at

        since = datetime.now(UTC) - timedelta(days=days)
        sql = "SELECT reason, coalesce(sum(input_tokens), 0), coalesce(sum(output_tokens), 0) FROM audit GROUP BY reason"
        async with audit_window(tenant, since, kinds=["llm"]) as db:
            rows = await asyncio.to_thread(lambda: db.execute(sql).fetchall())
        tokens: dict[str, list[int]] = {"generate": [0, 0], "classify": [0, 0], "verify": [0, 0]}
        for reason, tin, tout in rows:
            t = tokens[job_of(reason or "")]
            t[0] += tin
            t[1] += tout
        jobs = {
            "generate": ("Writing replies", rt.models.generate, "YACHAT_MODEL_GENERATE"),
            "classify": ("Reading messages", rt.models.classify, "YACHAT_MODEL_CLASSIFY"),
            "verify": ("Checking answers", rt.models.verifier, "YACHAT_MODEL_VERIFY"),
        }
        out = []
        for job, (label, model, setting) in jobs.items():
            tin, tout = tokens[job]
            current, _ = price(model, tin, tout)
            out.append(
                {
                    "job": job,
                    "label": label,
                    "setting": setting,
                    "model": model,
                    "input_tokens": tin,
                    "output_tokens": tout,
                    "cost_usd": current,
                    "options": cheaper(model, tin, tout),
                }
            )
        stt = os.getenv("VOICE_STT_MODEL") or ""
        out.append(
            {
                "job": "speech_to_text",
                "label": "Speech to text",
                "setting": "VOICE_STT_MODEL",
                "model": stt,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": None,
                "options": cheaper(stt.removeprefix("openrouter/"), 1000, 100, min_context=8_000, audio=True),
            }
        )
        fetched = prices_fetched_at()
        return {"days": days, "jobs": out, "prices_at": datetime.fromtimestamp(fetched, UTC).isoformat() if fetched else None}

    @app.post("/v1/tenants/{tenant}/usage/prices/refresh")
    async def refresh_model_prices(tenant: TenantId) -> dict[str, Any]:
        """Fetch OpenRouter's latest prices now (the server also does this daily). At most once a minute; prices
        apply to calls from now on, recorded costs keep the price they were made at."""
        from yachat.runtime.usage import prices_fetched_at, refresh_openrouter

        last = prices_fetched_at()
        if last and time.time() - last < 60:
            raise HTTPException(429, "Prices were fetched less than a minute ago; they are current.")
        n = await refresh_openrouter()
        if n == 0:
            raise HTTPException(502, "OpenRouter could not be reached; the previous prices are still in use")
        fetched = prices_fetched_at()
        return {"models": n, "openrouter_prices_at": datetime.fromtimestamp(fetched, UTC).isoformat() if fetched else None}

    async def workspace_db(tenant: str, days: int, agent: str | None) -> Any:
        """This tenant's audit records for the window, as locked read-only DuckDB tables."""
        try:
            from yachat.analytics.workspace_metrics import metrics_db
        except ImportError as e:  # vanna / pandas are in the analytics extra
            raise HTTPException(501, "key metrics need the analytics extra: uv sync --extra analytics") from e
        until = datetime.now(UTC)
        since = (until - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
        con = await audit_db(rt.audit, tenant, since, agent=agent)
        try:
            return await asyncio.to_thread(metrics_db, con)
        except BaseException:
            con.close()
            raise

    @app.get("/v1/tenants/{tenant}/metrics")
    async def workspace_metrics(tenant: TenantId, days: Annotated[int, Query(ge=1, le=90)] = 30, agent: str | None = None) -> dict[str, Any]:
        """This workspace's key metrics, each with its SQL, and the tables a question can be asked against."""
        from yachat.analytics.workspace_metrics import key_metrics

        db = await workspace_db(tenant, days, agent)
        try:
            values = await key_metrics(db)
            return {
                "days": days,
                "metrics": [
                    {"name": m.name, "label": m.label, "unit": m.unit, "description": m.description, "sql": m.sql, "value": v.value}
                    for v in values
                    for m in [v.metric]
                ],
                "tables": [{"name": t.name, "rows": t.rows, "description": t.description, "columns": [c for c, _ in t.columns]} for t in db.tables],
            }
        finally:
            db.close()

    @app.post("/v1/tenants/{tenant}/metrics/ask")
    async def ask_metrics(tenant: TenantId, body: MetricsQuestionIn) -> dict[str, Any]:
        """A plain-English question about this workspace, answered with SQL over its key-metric tables (read only,
        this workspace's rows only). Returns the SQL with the rows, so the answer can be checked."""
        from yachat.analytics.duck import AnalyticsError
        from yachat.analytics.workspace_metrics import ask

        db = await workspace_db(tenant, body.days, body.agent)
        try:
            async with spending(tenant) as models:
                a = await ask(db, rt.llm, models.generate, body.question)
        except AnalyticsError as e:
            log.info("metrics question not answered", tenant=tenant, kind=e.kind, error=str(e)[:300])
            raise HTTPException(422, "I couldn't turn that into a query over these tables. Try naming a metric, an agent or a time range.") from e
        except LLMError as e:
            log.error("metrics question failed", tenant=tenant, error=str(e)[:300])
            raise HTTPException(502, "The model could not be reached; try again.") from e
        finally:
            db.close()
        log.info("metrics question answered", tenant=tenant, attempts=a.attempts, rows=len(a.rows))
        return {
            "question": body.question,
            "sql": a.sql,
            "columns": [c.name for c in a.columns],
            "rows": a.rows,
            "truncated": a.truncated,
            "attempts": a.attempts,
        }

    @asynccontextmanager
    async def spending(tenant: str) -> AsyncIterator[ModelConfig]:
        """Model calls made for a workspace outside a conversation turn: refused once its plan is used up, run on the
        models its plan allows (yielded), then charged to it and listed on its cost page."""
        from yachat.runtime.usage import METER, Usage

        plan = await rt.budgets.get(tenant)
        if plan is not None and plan.exhausted():
            raise HTTPException(402, "This workspace has reached its usage limit.")
        meter: list[Usage] = []
        token = METER.set(meter)
        try:
            yield small_only(rt.models) if plan is not None and plan.small_models else rt.models
        finally:
            METER.reset(token)
            await rt.charge(tenant, meter)
            await record_usage(tenant, meter)

    async def record_usage(tenant: str, calls: list[Any]) -> None:
        """Model calls made for the workspace itself (not an agent) go on its cost page as agent "workspace"."""
        if not calls:
            return
        events = [
            AuditEvent(
                tenant=tenant,
                agent="workspace",
                kind="llm",
                decision="call",
                reason=c.purpose,
                ref=c.model,
                ms=c.ms,
                input_tokens=c.input_tokens,
                output_tokens=c.output_tokens,
                cached_tokens=c.cached_tokens,
                cost_usd=c.cost_usd,
                saved_tokens=c.saved_tokens or None,
            )
            for c in calls
        ]
        try:
            await rt.audit.append(events)
        except Exception as e:  # the answer is already computed
            rt.metrics.incr("audit_write_failed")
            log.error("audit write failed", tenant=tenant, error=f"{e.__class__.__name__}: {e}")

    @app.get("/v1/tenants/{tenant}/handoffs")
    async def list_handoffs(
        tenant: TenantId, status: HandoffStatus | None = None, agent: str | None = None, limit: Annotated[int, Query(ge=1, le=500)] = 100
    ) -> list[dict[str, Any]]:
        """The handoff inbox, newest first: conversations waiting for (or with) a person."""
        return [h.public() for h in await rt.handoffs.list(tenant, status=status, agent=agent, limit=limit)]

    @app.get("/v1/tenants/{tenant}/handoffs/{handoff_id}")
    async def get_handoff(tenant: TenantId, handoff_id: str) -> dict[str, Any]:
        h = await rt.handoffs.get(tenant, handoff_id)
        if h is None:
            raise NotFound(f"handoff {handoff_id!r}")
        return h.public()

    @app.post("/v1/tenants/{tenant}/handoffs/{handoff_id}")
    async def update_handoff(tenant: TenantId, handoff_id: str, body: HandoffUpdate, request: Request) -> dict[str, Any]:
        """Claim, resolve or annotate a handoff. Every change is in the audit log with who made it (a failed audit write is
        logged and counted as `audit_write_failed`; the change stands). Resolving gives the conversation back to the
        assistant unless `resume` is false."""
        actor = body.assignee or caller(request).key_id or "admin"
        if body.status is not None:
            h, _ = await report(tenant, handoff_id, body, actor=actor)
        elif (noted := await rt.handoffs.update(tenant, handoff_id, note=body.note)) is None:
            raise NotFound(f"handoff {handoff_id!r}")
        else:
            h = noted
            event = AuditEvent(
                tenant=h.tenant,
                agent=h.agent,
                policy_hash=h.policy_hash,
                session_id=h.session_id,
                kind="handoff",
                decision="noted",
                reason="note",
                ref=h.id,
                actor=actor,
            )
            await rt.audit_quietly([event])
        log.info("handoff updated", tenant=tenant, handoff=handoff_id, status=h.status)
        return h.public()

    async def report(tenant: str, handoff_id: str, body: HandoffUpdate, *, actor: str, handoff: Handoff | None = None) -> tuple[Handoff, bool]:
        assert body.status is not None
        try:
            out = await rt.report_handoff(
                tenant,
                handoff_id,
                status=body.status,
                resume=body.resume,
                fields=body.fields,
                assignee=body.assignee,
                note=body.note,
                actor=actor,
                handoff=handoff,
            )
        except ValueError as e:
            raise HTTPException(422, str(e)) from e
        if out is None:
            raise NotFound(f"handoff {handoff_id!r}")
        return out

    @app.post("/v1/public/handoffs/{tenant}/{handoff_id}/callback")
    async def handoff_callback(tenant: str, handoff_id: str, body: HandoffCallback, token: str = "") -> dict[str, Any]:
        """The receiver of a handoff reports back on the `callback_url` it was given (the token in it is the only
        credential, and opens only this handoff). `resolved` resumes the conversation unless `resume` is false."""
        from yachat.capabilities.nodes.handoff import token_hash

        h = await rt.handoffs.get(tenant, handoff_id)
        if h is None or not h.callback_hash or not token or not hmac.compare_digest(token_hash(token), h.callback_hash):
            raise NotFound(f"handoff {handoff_id!r}")  # same answer for a wrong token: nothing to probe
        updated, changed = await report(tenant, handoff_id, body, actor=body.assignee or "callback", handoff=h)
        return {"id": updated.id, "status": updated.status, "conversation": ("resumed" if body.resume else "ended") if changed else "unchanged"}

    @app.get("/v1/metrics")
    async def metrics(request: Request) -> dict[str, Any]:
        """Latency across every tenant's traffic: the admin key only."""
        if not caller(request).admin:
            raise HTTPException(403, "metrics cover every tenant; use the admin key")
        return rt.metrics.snapshot()

    @app.post("/v1/tenants/{tenant}/agents/{agent}/embeds", status_code=201)
    async def publish_agent(tenant: TenantId, agent: str, body: EmbedIn, request: Request) -> dict[str, Any]:
        """Publish an agent to the web: a website widget and an installable app. Returns the public id, the snippet to
        paste into a site and the app link. Only the listed sites can show the widget."""
        if await rt.policies.latest(agent, tenant) is None:
            raise NotFound(f"no saved policy for agent {agent!r}; save a version first")
        e = await embed_store.put(Embed(tenant=tenant, agent=agent, **body.model_dump()))
        log.info("agent published", tenant=tenant, agent=agent, embed=e.id, origins=len(e.allowed_origins))
        return embed_view(e, request)

    @app.get("/v1/tenants/{tenant}/embeds")
    async def list_embeds(tenant: TenantId, request: Request) -> list[dict[str, Any]]:
        return [embed_view(e, request) for e in await embed_store.list(tenant)]

    @app.post("/v1/tenants/{tenant}/embeds/{embed_id}")
    async def update_embed(tenant: TenantId, embed_id: str, body: EmbedUpdate, request: Request) -> dict[str, Any]:
        """Turn a published agent off or on, or change its sites, title, greeting or daily limit."""
        e = await embed_store.get(embed_id)
        if e is None or e.tenant != tenant:
            raise NotFound(f"embed {embed_id!r}")
        e = await embed_store.put(e.model_copy(update=body.model_dump(exclude_none=True)))
        return embed_view(e, request)

    async def survey_questions(tenant: str, agent: str) -> list[SurveyQuestion]:
        policy = await rt.policies.latest(agent, tenant)
        if policy is None:
            raise NotFound(f"no policy for agent {agent!r}")
        from yachat.capabilities.nodes.survey_answers import survey_questions_of

        questions = survey_questions_of(policy.steps)
        if not questions:
            raise HTTPException(422, f"agent {agent!r} has no survey step")
        return questions

    @app.get("/v1/tenants/{tenant}/plan")
    async def get_plan(tenant: TenantId) -> dict[str, Any]:
        """The workspace's plan and what it has used; `plan: null` means unlimited (self-hosted or admin-managed)."""
        plan = await rt.budgets.get(tenant)
        return {"tenant": tenant, "plan": asdict(plan) if plan else None}

    @app.put("/v1/tenants/{tenant}/plan")
    async def set_plan(tenant: TenantId, body: PlanIn, request: Request) -> dict[str, Any]:
        """Set a workspace's plan (admin key only): limits, small models only, paid features. Usage so far is kept."""
        if not caller(request).admin:
            raise HTTPException(403, "only the server admin can change a plan")
        await rt.budgets.set_plan(tenant, body.limit_turns, body.limit_usd, body.small_models, body.paid)
        log.info("plan set", tenant=tenant, paid=body.paid, limit_turns=body.limit_turns, small_models=body.small_models)
        return await get_plan(tenant)

    job_store = jobs or job_store_from_env()
    job_tasks: set[asyncio.Task[None]] = set()
    #: a job still "running" after this long died with its machine (a restart mid-run)
    JOB_STALE = timedelta(minutes=15)

    async def run_refine(job: InsightJob) -> None:
        from yachat.sim.refine import refine

        try:
            async with spending(job.tenant) as models:
                catalog = {d.name: d.kind for d in await rt.datasets.list(job.tenant)}
                r = await refine(rt.llm, models, job.request["yaml"], job.tenant, catalog, rounds=int(job.request.get("rounds", 2)))
            job.status, job.result = "done", r.model_dump(mode="json")
        except PolicyValidationError as e:
            job.status, job.error = "failed", "; ".join(f"{x.path}: {x.message}" for x in e.errors)[:500]
        except (HTTPException, ValueError) as e:
            job.status, job.error = "failed", str(getattr(e, "detail", e))[:300]
        except Exception as e:  # vendor text is logged, never shown
            log.error("refine job failed", job=job.id, error=f"{e.__class__.__name__}: {e}"[:300])
            job.status, job.error = "failed", "The test couldn't finish. Try again in a minute."
        job.finished_at = datetime.now(UTC)
        await job_store.put(job)
        log.info("refine job finished", tenant=job.tenant, job=job.id, status=job.status)

    @app.post("/v1/tenants/{tenant}/agents/refine", status_code=202)
    async def start_refine(tenant: TenantId, body: RefineIn) -> dict[str, Any]:
        """Test a survey with pretend customers and improve it (in the background): each round is measured in code
        (finished, each objective answered, no getting stuck) into a confidence score, and a revision aimed at what went
        wrong is played again, up to `rounds` plays. Poll GET .../refine/{id}; nothing is saved or published."""
        catalog = {d.name: d.kind for d in await rt.datasets.list(tenant)}
        try:
            build(body.yaml, tenant=tenant, datasets=catalog)
        except PolicyValidationError as e:
            raise HTTPException(422, "; ".join(f"{x.path}: {x.message}" for x in e.errors)) from None
        plan = await rt.budgets.get(tenant)
        if plan is not None and plan.exhausted():
            raise HTTPException(402, "This workspace has reached its usage limit.")
        job = InsightJob(tenant=tenant, request={"kind": "refine", **body.model_dump(mode="json")})
        await job_store.put(job)
        task = asyncio.create_task(run_refine(job))
        job_tasks.add(task)
        task.add_done_callback(job_tasks.discard)
        return {"id": job.id, "status": job.status}

    @app.get("/v1/tenants/{tenant}/agents/refine/{job_id}")
    async def get_refine(tenant: TenantId, job_id: str) -> dict[str, Any]:
        job = await job_or_404(tenant, job_id)
        if job.request.get("kind") != "refine":
            raise NotFound(f"test {job_id!r}")
        out: dict[str, Any] = job.model_dump(mode="json", exclude={"request", "words"})
        return out

    async def job_or_404(tenant: str, job_id: str) -> InsightJob:
        job = await job_store.get(tenant, job_id)
        if job is None:
            raise NotFound(f"insights job {job_id!r}")
        stale = JOB_STALE * 3 if job.request.get("kind") == "refine" else JOB_STALE  # a test plays several conversations
        if job.status == "running" and datetime.now(UTC) - job.created_at > stale:
            job.status, job.error = "failed", "The analysis was interrupted. Start it again."
        return job

    from yachat.runtime.findings import Findings as SurveyFindings
    from yachat.runtime.findings import Recommendations

    findings_cache: dict[tuple[str, str, int], tuple[float, str, SurveyFindings, Recommendations | None]] = {}

    async def cached_findings(tenant: str, agent: str, days: int, with_recommendations: bool = False) -> tuple[SurveyFindings, Recommendations | None]:
        """Findings (and recommendations) for these answers, built once: the themes cost model calls, so the same answers
        viewed again within 10 minutes reuse them. New answers build them again."""
        from yachat.runtime.findings import recommendations

        questions = await survey_questions(tenant, agent)
        rs = await rt.responses.list(tenant, agent, since=datetime.now(UTC) - timedelta(days=days))
        stamp = f"{len(rs)}:{max((r.completed_at for r in rs), default=datetime.min.replace(tzinfo=UTC)).isoformat()}"
        hit = findings_cache.get((tenant, agent, days))
        if hit and hit[1] == stamp and time.monotonic() - hit[0] < 600 and (hit[3] is not None or not with_recommendations):
            return hit[2], hit[3]
        async with spending(tenant) as models:
            found = hit[2] if hit and hit[1] == stamp and time.monotonic() - hit[0] < 600 else await findings(agent, questions, rs, rt.llm, models.generate)
            recs = await recommendations(found, questions, rs, rt.llm, models.generate) if with_recommendations else None
        if len(findings_cache) > 200:
            findings_cache.clear()
        findings_cache[(tenant, agent, days)] = (time.monotonic(), stamp, found, recs)
        return found, recs

    @app.get("/v1/tenants/{tenant}/agents/{agent}/findings")
    async def survey_findings(tenant: TenantId, agent: str, days: Annotated[int, Query(ge=1, le=365)] = 30) -> dict[str, Any]:
        """What a survey found: counts, averages and NPS computed in code from the responses (only people who reached a
        question count toward it), and themes for open questions, each backed by people's own words."""
        found, _ = await cached_findings(tenant, agent, days)
        return found.model_dump(mode="json") | {"days": days}

    @app.get("/v1/tenants/{tenant}/agents/{agent}/findings/recommendations")
    async def survey_recommendations(tenant: TenantId, agent: str, days: Annotated[int, Query(ge=1, le=365)] = 30) -> dict[str, Any]:
        """What to fix first and what to keep doing: themes ranked by the unhappy customers who raised them (counted in
        code), each with that group's score and their own words; the model only words the action."""
        _, recs = await cached_findings(tenant, agent, days, with_recommendations=True)
        assert recs is not None
        return recs.model_dump(mode="json") | {"days": days}

    @app.get("/v1/tenants/{tenant}/agents/{agent}/findings/aggregates")
    async def survey_aggregates(
        tenant: TenantId,
        agent: str,
        days: Annotated[int, Query(ge=1, le=730)] = 180,
        by: Annotated[Literal["week", "month"], Query()] = "week",
    ) -> dict[str, Any]:
        """The survey's score (NPS, or the average) per week or month, and split by campaign, channel, language, product
        and first-time vs repeat buyer. Counted in code from every response, finished or not; no model is called."""
        from yachat.runtime.findings import aggregates

        questions = await survey_questions(tenant, agent)
        rs = await rt.responses.list(tenant, agent, since=datetime.now(UTC) - timedelta(days=days), limit=AGGREGATE_LIMIT)
        # past the limit, only the newest responses are counted: say so rather than show a partial total as the whole
        return aggregates(questions, rs, by).model_dump(mode="json") | {"days": days, "truncated": len(rs) >= AGGREGATE_LIMIT}

    @app.get("/v1/tenants/{tenant}/agents/{agent}/responses.csv")
    async def survey_export(tenant: TenantId, agent: str, days: Annotated[int, Query(ge=1, le=365)] = 365) -> Response:
        """Every response as CSV, finished or not (its `complete` column says), one row each, the newest 10,000 at most
        (cells that start like a formula are quoted for spreadsheets)."""
        import csv
        import io

        questions = await survey_questions(tenant, agent)
        rs = await rt.responses.list(tenant, agent, since=datetime.now(UTC) - timedelta(days=days))
        buf = io.StringIO()
        w = csv.writer(buf)
        for row in csv_rows(questions, rs):
            w.writerow(["'" + c if c[:1] in ("=", "+", "-", "@", "\t", "\r") else c for c in row])
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{agent}-responses.csv"'})

    @app.post("/v1/tenants/{tenant}/assistants", status_code=201)
    async def create_assistant(
        tenant: TenantId,
        request: Request,
        name: Annotated[str, Form(min_length=2, max_length=60)],
        urls: Annotated[str, Form(max_length=5_000)] = "",
        greeting: Annotated[str, Form(max_length=300)] = "",
        allowed_origins: Annotated[str, Form(max_length=2_000)] = "",
        files: Annotated[list[UploadFile] | None, File()] = None,
        kind: Annotated[AssistantKind, Form()] = "answers",
        description: Annotated[str, Form(max_length=2_000)] = "",
        languages: Annotated[str, Form(pattern=r"^\s*([a-z]{2}(\s+|$))*$", max_length=60)] = "",
        draft_only: Annotated[bool, Form()] = False,
    ) -> dict[str, Any]:
        """A chatbot from your content: upload files (PDF, text, Markdown, CSV, HTML) and/or give web page addresses.
        The content becomes a document set and the chatbot's policy is made from it: in code for a plain question
        answerer, or drafted by the model from a starting type (`kind`) and the owner's own words (`description`).
        With `draft_only` the policy is returned for review with a plain-words summary (publish it with POST
        .../assistants/publish); otherwise it is saved and published, and the response lists every channel to put it
        on (website, Shopify, WordPress, link, WhatsApp, phone)."""
        from yachat.datasets.models import DocumentSource, UnstructuredDataset
        from yachat.datasets.uploads import MAX_FILE_BYTES, MAX_FILES, MAX_URLS, UploadRejected, fetch_page, file_text, slug

        store = artifacts
        if store is None:
            raise HTTPException(501, "uploads need YACHAT_UPLOADS_BUCKET (S3) or YACHAT_DATA_ROOT on the server")
        agent = slug(name, 40)
        if not agent[0].isalnum():
            agent = "bot-" + agent
        links = [u.strip() for u in re.split(r"[\s,]+", urls) if u.strip()]
        uploads = files or []
        if not links and not uploads and kind in ("answers", "support"):
            raise HTTPException(422, "add at least one file or web page")
        if len(uploads) > MAX_FILES or len(links) > MAX_URLS:
            raise HTTPException(422, f"at most {MAX_FILES} files and {MAX_URLS} web pages")
        docs: list[tuple[str, str]] = []
        try:
            for f in uploads:
                data = await f.read(MAX_FILE_BYTES + 1)
                docs.append((f.filename or "file", await asyncio.to_thread(file_text, f.filename or "file", data)))
            for u in links:
                docs.append(await fetch_page(u))
        except UploadRejected as err:
            raise HTTPException(422, str(err)) from None
        sources = []
        for n, (title, text) in enumerate(docs, 1):
            key = f"uploads/{tenant}/{agent}/{n:02d}-{slug(title)}.md"  # the same name again replaces that chatbot's content
            uri = await store.put(key, f"# {title}\n\n{text}".encode(), content_type="text/markdown; charset=utf-8", replace=True)
            sources.append(DocumentSource(uri=uri, title=title[:500], format="markdown"))
        dataset_name = ("kb_" + agent.replace("-", "_"))[:63] if docs else ""
        if docs:
            await put_dataset(tenant, dataset_name, DatasetIn(spec=UnstructuredDataset(documents=sources), description=f"Content for {name}"))
        langs = languages.split() or ["en"]
        catalog = {d.name: d.kind for d in await rt.datasets.list(tenant)}
        made = {"agent": agent, "dataset": dataset_name, "documents": [t for t, _ in docs]}
        if kind == "answers" and not description.strip():
            policy_yaml, errors = answers_policy(agent, name, dataset_name, [t for t, _ in docs], langs), list[PolicyError]()
        else:
            async with spending(tenant) as models:
                policy_yaml, errors = await drafted_policy(tenant, agent, name, kind, description, dataset_name, langs, catalog, models)
        if draft_only:
            summary = [] if errors else plain_summary(build(policy_yaml, tenant=tenant, datasets=catalog))
            return made | {"yaml": policy_yaml, "summary": summary, "errors": [{"path": e.path, "message": e.message} for e in errors]}
        if errors:
            raise HTTPException(422, "; ".join(f"{e.path}: {e.message}" for e in errors))
        return made | await publish_assistant(
            tenant, request, name, policy_yaml, greeting, re.split(r"[\s,]+", allowed_origins), "created from uploaded content"
        )

    def answers_policy(agent: str, name: str, dataset: str, titles: list[str], langs: list[str]) -> str:
        """A question answerer over the uploaded content, written in code (no model)."""
        # the scope check reads only the charter, so name every document; otherwise a page on another subject looks off-topic
        charter = (
            f"Answer questions about {name} and about the topics of its documents ({'; '.join(t[:80] for t in titles)}), using only "
            "those documents. Friendly and concise. When the documents don't cover a question, say so and offer to pass it to a "
            "person. Out of scope: anything unrelated to these documents."
        )
        return yaml.safe_dump(
            {
                "agent": agent,
                "charter": charter,
                **({"languages": langs} if langs != ["en"] else {}),
                "flow": [{"knowledge": {"dataset": dataset}}],
                "guardrails": {"input": ["prompt_injection"], "output": ["pii"]},
                "handoff": {"when": ["user_request"]},
            },
            sort_keys=False,
        )

    async def drafted_policy(
        tenant: str, agent: str, name: str, kind: str, description: str, dataset: str, langs: list[str], catalog: dict[str, Any], models: ModelConfig
    ) -> tuple[str, list[PolicyError]]:
        """The model drafts the policy from the starting type's template and the owner's words; the build checks it."""
        base = next((t.yaml for t in builtin_templates() if t.id == ASSISTANT_TEMPLATES[kind]), "")
        request_text = (
            f"A chatbot for {name}. Start from this template and change only what the owner asks; keep its steps otherwise:\n"
            f"{base}\n"
            + (
                f"The owner's own content is the unstructured dataset `{dataset}`. Answer questions from it with a `knowledge` "
                f"step that names it (as the last step when the flow has other steps), and drop `knowledge_search` from tools.\n"
                if dataset
                else ""
            )
            + f"Languages: {', '.join(langs)}.\n"
            + f"What the owner wants: {description.strip() or 'what the template does, for this business'}"
        )
        draft = await draft_policy(rt.llm, models, request_text, tenant, catalog)
        text = renamed(draft.yaml, agent) if not draft.errors else draft.yaml
        if draft.errors:
            return text, draft.errors
        try:
            build(text, tenant=tenant, datasets=catalog)
        except PolicyValidationError as e:
            return text, e.errors
        return text, []

    async def publish_assistant(tenant: str, request: Request, name: str, policy_yaml: str, greeting: str, origins: list[str], note: str) -> dict[str, Any]:
        """Save the policy as the agent's next version and publish it as a chatbot (reusing its chatbot if it has one)."""
        catalog = {d.name: d.kind for d in await rt.datasets.list(tenant)}
        try:
            policy = build(policy_yaml, tenant=tenant, datasets=catalog)
        except PolicyValidationError as err:
            raise HTTPException(422, str(err)) from None
        await rt.save_version(policy, policy_yaml, note=note)
        try:
            embed_in = EmbedIn(name=name, title=name[:60], greeting=greeting or f"Hi! Ask me anything about {name}.", allowed_origins=[o for o in origins if o])
        except ValidationError as err:
            raise HTTPException(422, "; ".join(str(x["msg"]).removeprefix("Value error, ") for x in err.errors())) from None
        published = await embed_store.put(Embed(tenant=tenant, agent=policy.agent, **embed_in.model_dump()))
        log.info("assistant published", tenant=tenant, agent=policy.agent, embed=published.id)
        return {"agent": policy.agent} | embed_view(published, request)

    @app.post("/v1/tenants/{tenant}/assistants/publish", status_code=201)
    async def publish_reviewed_assistant(tenant: TenantId, body: PublishIn, request: Request) -> dict[str, Any]:
        """Publish a drafted chatbot after the owner reviewed (and maybe edited) its policy."""
        return await publish_assistant(tenant, request, body.name, body.yaml, body.greeting, body.allowed_origins, "reviewed and published")

    @app.post("/v1/tenants/{tenant}/embeds/{embed_id}/whatsapp")
    async def connect_whatsapp(tenant: TenantId, embed_id: str, body: WhatsAppIn, request: Request) -> dict[str, Any]:
        """Connect a WhatsApp Business number (Meta's WhatsApp Cloud API) to a published agent. The access token and app
        secret are stored encrypted and never returned; set the webhook URL and verify token in Meta's dashboard."""
        from yachat.channels.whatsapp_cloud import WhatsAppError, seal

        e = await embed_store.get(embed_id)
        if e is None or e.tenant != tenant:
            raise NotFound(f"embed {embed_id!r}")
        try:
            e.wa_phone_number_id, e.wa_token, e.wa_app_secret = body.phone_number_id, seal(body.access_token), seal(body.app_secret)
        except WhatsAppError as err:
            raise HTTPException(503, str(err)) from None
        e.wa_verify_token = secrets.token_urlsafe(24)
        await embed_store.put(e)
        log.info("whatsapp connected", tenant=tenant, embed=e.id)
        return {"webhook_url": f"{public_base(request)}/v1/public/whatsapp/{e.id}", "verify_token": e.wa_verify_token} | embed_view(e, request)

    @app.post("/v1/tenants/{tenant}/embeds/{embed_id}/check")
    async def check_install(tenant: TenantId, embed_id: str, body: InstallCheckIn) -> dict[str, Any]:
        """Is the chatbot on this page? Loads the public page and looks for the chatbot's line, and says whether the
        site is one the chatbot may appear on."""
        from urllib.parse import urlparse

        from yachat.datasets import uploads

        e = await embed_store.get(embed_id)
        if e is None or e.tenant != tenant:
            raise NotFound(f"embed {embed_id!r}")
        try:
            final, page, _, encoding = await uploads.fetch_raw(body.url.strip(), agent="yachatBot/1.0 (+install check)")
        except uploads.UploadRejected as err:
            raise HTTPException(422, str(err)) from None
        u = urlparse(final)
        origin = f"{u.scheme}://{u.netloc}".lower()
        installed = f'data-agent="{e.id}"' in page.decode(encoding, errors="replace")
        allowed = origin in e.allowed_origins
        if installed and allowed:
            message = "Your chatbot is on this page. Open the page to see the chat button in the corner."
        elif installed:
            message = (
                f"The chatbot's line is on the page, but {origin} isn't one of its allowed websites, so it stays hidden. Add {origin} to allowed websites."
            )
        elif u.path.rstrip("/").endswith("/password"):
            message = (
                "This store is password-protected, so the check can't see it. Remove the password (Online Store, then Preferences) or check again after launch."
            )
        else:
            message = (
                "We loaded the page but didn't find your chatbot's line. "
                "Make sure you pasted it and saved; if your site uses a cache, clear it and check again."
            )
        return {"installed": installed, "allowed": allowed, "url": final, "message": message}

    def embed_view(e: Embed, request: Request) -> dict[str, Any]:
        base = public_base(request)
        return e.view() | {"snippet": snippet(base, e), "app_url": f"{base}/a/{e.id}", "channels": channels(base, e)}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    from fastapi.staticfiles import StaticFiles

    if DIST.is_dir():  # built console, served same-origin under /console/ (vite base)
        app.mount("/console", StaticFiles(directory=str(DIST), html=True), name="frontend")

    @app.get("/", include_in_schema=False)
    async def home() -> Response:
        """The test console when it is built, else the API docs."""
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/console/" if DIST.is_dir() else "/docs")

    return app

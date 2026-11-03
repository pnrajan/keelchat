"""yachat as a remote MCP server at /mcp: Claude (or any MCP client) builds, checks, tries and launches agents for
the tenant its key belongs to.

Every tool forwards to the HTTP API in-process with the caller's own `Authorization` header, so the API's checks
(the key, its tenant, policy validation, only valid builds saved) apply unchanged; the tools add nothing to trust."""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.types import ASGIApp, Receive, Scope, Send

INSTRUCTIONS = """Build goal-oriented chat agents on this yachat server, for the tenant your key belongs to.
Workflow: whoami → list_bot_types (or draft_policy from a description) → create_agent → get_policy / update_policy
→ check_readiness until nothing blocks → try_conversation → save_version → activate_version to roll back.
Policies are YAML; get_schema lists every key, step and tool. Only valid builds are saved."""


class _Api:
    def __init__(self, app: FastAPI):
        self.app = app

    async def __call__(self, ctx: Context[Any, Any], method: str, path: str, body: Any = None) -> Any:
        auth = (ctx.headers or {}).get("authorization", "")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://yachat", timeout=300) as c:
            r = await c.request(method, path, json=body, headers={"Authorization": auth} if auth else {})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        if r.status_code >= 400:
            raise ToolError(f"{r.status_code}: {json.dumps(data.get('detail', data) if isinstance(data, dict) else data)}")
        return data


def build_mcp(app: FastAPI) -> MCPServer:
    api = _Api(app)
    mcp: MCPServer = MCPServer("yachat", instructions=INSTRUCTIONS)

    async def tenant(ctx: Context[Any, Any]) -> str:
        return str((await api(ctx, "GET", "/v1/whoami"))["tenant"])

    @mcp.tool()
    async def whoami(ctx: Context[Any, Any]) -> dict[str, Any]:
        """The tenant this connection acts as."""
        out: dict[str, Any] = await api(ctx, "GET", "/v1/whoami")
        return out

    @mcp.tool()
    async def get_schema(ctx: Context[Any, Any]) -> dict[str, Any]:
        """The policy JSON Schema: every key, flow step (with its settings) and tool this server offers."""
        s: dict[str, Any] = await api(ctx, "GET", "/v1/schema")
        return {k: v for k, v in s.items() if k != "x-registry"}

    @mcp.tool()
    async def list_bot_types(ctx: Context[Any, Any]) -> list[dict[str, Any]]:
        """Starting points for a new agent: built-in types and this tenant's own."""
        t = await tenant(ctx)
        return [{k: x[k] for k in ("id", "name", "description", "builtin")} for x in await api(ctx, "GET", f"/v1/tenants/{t}/templates")]

    @mcp.tool()
    async def list_agents(ctx: Context[Any, Any]) -> list[dict[str, Any]]:
        """This tenant's saved agents with their active and latest versions."""
        out: list[dict[str, Any]] = await api(ctx, "GET", f"/v1/tenants/{await tenant(ctx)}/agents")
        return out

    @mcp.tool()
    async def draft_policy(description: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Draft a policy YAML from a plain description (checked by the build; not saved). Review it, then create_agent."""
        out: dict[str, Any] = await api(ctx, "POST", "/v1/agents/draft", {"description": description})
        return out

    @mcp.tool()
    async def create_agent(agent: str, ctx: Context[Any, Any], bot_type: str = "", copy_from: str = "", policy_yaml: str = "") -> dict[str, Any]:
        """Create a new agent as v1 (active): from a bot type id, a copy of an existing agent, or your own YAML
        (its `agent:` is replaced by `agent`). Give exactly one of bot_type, copy_from, policy_yaml."""
        t = await tenant(ctx)
        if sum(map(bool, (bot_type, copy_from, policy_yaml))) != 1:
            raise ToolError("give exactly one of bot_type, copy_from, policy_yaml")
        if bot_type:
            out: dict[str, Any] = await api(ctx, "POST", f"/v1/tenants/{t}/agents/from-template", {"template": bot_type, "agent": agent})
        elif copy_from:
            out = await api(ctx, "POST", f"/v1/tenants/{t}/agents/{copy_from}/copy", {"agent": agent})
        else:
            from yachat.policy.templates import renamed

            out = await api(ctx, "POST", f"/v1/tenants/{t}/agents", {"yaml": renamed(policy_yaml, agent), "note": "created from Claude"})
        return out

    @mcp.tool()
    async def get_policy(agent: str, ctx: Context[Any, Any], version: int = 0) -> dict[str, Any]:
        """An agent's policy YAML: the active version, or `version`."""
        t = await tenant(ctx)
        if not version:
            a = next((x for x in await api(ctx, "GET", f"/v1/tenants/{t}/agents") if x["agent"] == agent), None)
            if a is None:
                raise ToolError(f"no agent {agent!r}")
            version = a["active_version"] or a["latest_version"]
        out: dict[str, Any] = await api(ctx, "GET", f"/v1/tenants/{t}/agents/{agent}/versions/{version}")
        return out

    @mcp.tool()
    async def check_readiness(policy_yaml: str, ctx: Context[Any, Any]) -> dict[str, Any]:
        """What the policy still needs before launch (build errors, unconnected tools, consent, scope, saved version,
        tests), each with a status and whether it blocks launch."""
        out: dict[str, Any] = await api(ctx, "POST", f"/v1/tenants/{await tenant(ctx)}/agents/readiness", {"yaml": policy_yaml})
        return out

    @mcp.tool()
    async def save_version(policy_yaml: str, ctx: Context[Any, Any], note: str = "", activate: bool = True) -> dict[str, Any]:
        """Save a valid policy as the agent's next version (its `agent:` names the agent); active by default.
        Invalid policies are rejected with their YAML paths."""
        body = {"yaml": policy_yaml, "note": note, "activate": activate}
        out: dict[str, Any] = await api(ctx, "POST", f"/v1/tenants/{await tenant(ctx)}/agents", body)
        return out

    @mcp.tool()
    async def activate_version(agent: str, version: int, ctx: Context[Any, Any]) -> dict[str, Any]:
        """Make a saved version the one new sessions use (also a rollback). Running sessions keep theirs."""
        out: dict[str, Any] = await api(ctx, "POST", f"/v1/tenants/{await tenant(ctx)}/agents/{agent}/versions/{version}/activate")
        return out

    @mcp.tool()
    async def try_conversation(agent: str, messages: list[str], ctx: Context[Any, Any]) -> dict[str, Any]:
        """Talk to an agent's active version as a test user, one message per turn; returns each reply and its route."""
        s = await api(ctx, "POST", "/v1/sessions", {"agent": agent, "user_id": "claude-test", "channel": "web"})
        turns = []
        for m in messages[:12]:
            r = await api(ctx, "POST", f"/v1/sessions/{s['id']}/turns", {"text": m})
            turns.append({"user": m, "agent": r["outbound"]["text"], "step": r.get("route"), "errors": r.get("errors")})
        return {"session": s["id"], "policy_hash": s["policy_hash"], "turns": turns}

    return mcp


class RequireKey:
    """The MCP endpoint needs the same key as the API, before any MCP message is read."""

    def __init__(self, inner: ASGIApp, app: FastAPI):
        self.inner, self.app = inner, app  # the key store is read per request (app.state.keys)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        from yachat.api.keys import resolve

        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            if await resolve(auth or None, self.app.state.keys) is None:
                body = b'{"detail": "missing or invalid API key"}'
                await send({"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.inner(scope, receive, send)

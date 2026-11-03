"""MCP tool adapter: every tool of every configured MCP server becomes a registry tool named `<alias>.<tool>`.

`YACHAT_MCP_SERVERS` lists servers as `alias=target` pairs separated by commas. A target starting with `http`
is a streamable-HTTP endpoint; anything else is a command line started over stdio. Servers are contacted at discovery
(startup and build); a server that cannot be reached is logged and skipped so the platform still starts. Calls run
through the Tool Gateway like any other tool: allow list, timeout, retries, breaker, scrub, untrusted output.

Each call opens its own session. For an HTTP server that is one request; for a stdio server it starts the command
every call, so production deployments should prefer HTTP servers.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from yachat.capabilities.tools import ToolError
from yachat.logging import get_logger
from yachat.registry.core import Registry, ToolFn, ToolSpec, model_from_json_schema

log = get_logger("mcp")

ENV = "YACHAT_MCP_SERVERS"


def servers_from_env() -> dict[str, str]:
    out: dict[str, str] = {}
    for item in filter(None, (s.strip() for s in os.getenv(ENV, "").split(","))):
        alias, sep, target = item.partition("=")
        if not sep or not alias.strip() or not target.strip():
            raise ValueError(f"{ENV}: expected alias=target, got {item!r}")
        out[alias.strip()] = target.strip()
    return out


@asynccontextmanager
async def session(target: str) -> AsyncIterator[Any]:
    from mcp import ClientSession

    if target.startswith("http"):
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(target) as streams, ClientSession(streams[0], streams[1]) as s:
            await s.initialize()
            yield s
    else:
        from mcp.client.stdio import StdioServerParameters, stdio_client

        command, *args = shlex.split(target)
        async with stdio_client(StdioServerParameters(command=command, args=args)) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            yield s


def _result_data(result: Any) -> Any:
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    return texts[0] if len(texts) == 1 else texts


def _tool_fn(name: str, target: str, remote: str) -> ToolFn:
    async def call(**args: Any) -> Any:
        try:
            async with session(target) as s:
                result = await s.call_tool(remote, args)
        except Exception as e:  # transport, protocol or process failure: structured for the gateway
            raise ToolError(name, "upstream", e.__class__.__name__) from e
        if getattr(result, "is_error", False):
            raise ToolError(name, "upstream", "server returned an error")
        return _result_data(result)

    return call


async def _listed(target: str) -> Any:
    async with session(target) as s:
        return await s.list_tools()


async def list_tools(alias: str, target: str, *, timeout_s: float) -> list[ToolSpec]:
    listed = await asyncio.wait_for(_listed(target), timeout=timeout_s)  # a server that never answers is cut off
    specs = []
    for t in listed.tools:
        name = f"{alias}.{t.name}"
        hints = t.annotations
        # MCP's own hint: a destructive tool is a write (no retries, never offered to the planner)
        writes = bool(hints is not None and hints.destructive_hint)
        specs.append(
            ToolSpec(
                name=name,
                fn=_tool_fn(name, target, t.name),
                input_model=model_from_json_schema(name, t.input_schema or {}),
                output_schema=t.output_schema or {},
                timeout_s=timeout_s,
                retries=0 if writes else 1,
                side_effects="write" if writes else "none",
                description=t.description or t.title or "",
                origin="mcp",
            )
        )
    return specs


async def _list_all(servers: dict[str, str], timeout_s: float) -> list[list[ToolSpec] | BaseException]:
    return await asyncio.gather(*(list_tools(a, t, timeout_s=timeout_s) for a, t in servers.items()), return_exceptions=True)


def register_mcp_servers(registry: Registry, servers: dict[str, str] | None = None, *, timeout_s: float = 10.0) -> list[str]:
    """Sync entry point for discovery (which may run inside an event loop): all servers are listed concurrently on one
    loop in a worker thread, bounded by `timeout_s`. Runs once, at startup (Runtime and the CLI call discover())."""
    try:
        servers = servers_from_env() if servers is None else servers
    except ValueError as e:  # bad configuration is logged; the platform still starts with its own tools
        log.error("mcp configuration invalid", error=str(e))
        return []
    if not servers:
        return []
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        results = pool.submit(asyncio.run, _list_all(servers, timeout_s)).result(timeout=timeout_s + 5)
    except Exception as e:
        log.error("mcp servers unavailable", servers=sorted(servers), error=f"{e.__class__.__name__}: {e}")
        return []
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    registered: list[str] = []
    for alias, result in zip(servers, results, strict=True):
        if isinstance(result, BaseException):  # a server that is down must not take the platform down
            log.error("mcp server unavailable", server=alias, error=f"{result.__class__.__name__}: {result}")
            continue
        for spec in result:
            registry.add_tool(spec)
            registered.append(spec.name)
        log.info("mcp tools registered", server=alias, tools=[s.name for s in result])
    return registered

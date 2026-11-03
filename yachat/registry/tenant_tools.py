"""Tenant tools: an HTTP endpoint a customer registers through the Tool Registry API.

Visible only to that tenant's agents. The gateway POSTs the validated arguments as JSON and treats the JSON reply as
data, never as instructions. Registered specs pass the same contract suite as built-in tools.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from yachat.capabilities.tools import post_json
from yachat.registry.core import ToolFn, ToolSpec, model_from_json_schema


class TenantToolIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=1, max_length=500)
    url: str = Field(pattern=r"^https?://")
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] = Field(default_factory=dict)
    timeout_s: float = Field(default=5.0, gt=0, le=60)
    retries: int = Field(default=1, ge=0, le=3)
    #: "write" for endpoints that change something: never retried, never offered to the planner
    side_effects: Literal["none", "write"] = "none"
    #: sent on every call (for example an Authorization header); never returned by the API
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _checks(self) -> TenantToolIn:
        if self.side_effects == "write" and self.retries:
            raise ValueError("a write endpoint cannot be retried (set retries: 0)")
        props = self.input_schema.get("properties") or {}
        bad = [p for p in props if not p.isidentifier() or p.startswith("_") or p.startswith("model_")]
        if bad:
            raise ValueError(f"input_schema property names must be identifiers not starting with '_' or 'model_': {bad}")
        return self


class TenantToolRejected(ValueError):
    pass


def check_public_url(url: str) -> None:
    """Tenant endpoints must resolve to public addresses: no loopback, private, link-local or metadata hosts.
    YACHAT_ALLOW_PRIVATE_TOOL_URLS=1 lifts this for local development."""
    if os.getenv("YACHAT_ALLOW_PRIVATE_TOOL_URLS") == "1":
        return
    host = urlparse(url).hostname or ""
    try:
        addrs = {str(info[4][0]) for info in socket.getaddrinfo(host, None)}
    except OSError as e:
        raise TenantToolRejected(f"cannot resolve {host!r}") from e
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global:
            raise TenantToolRejected(f"{host!r} resolves to a non-public address")


def _tool_fn(name: str, url: str, headers: dict[str, str], timeout_s: float) -> ToolFn:
    async def call(**args: Any) -> Any:
        return await post_json(name, url, args, timeout_s, headers)

    return call


def tenant_tool_spec(tenant: str, body: TenantToolIn) -> ToolSpec:
    return ToolSpec(
        name=body.name,
        fn=_tool_fn(body.name, body.url, body.headers, body.timeout_s),
        input_model=model_from_json_schema(f"{tenant}_{body.name}", body.input_schema),
        output_schema=body.output_schema,
        timeout_s=body.timeout_s,
        retries=body.retries,
        side_effects=body.side_effects,
        description=body.description,
        tenant=tenant,
        url=body.url,
    )

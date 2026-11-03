"""Error reporting and performance traces in Sentry (the free Developer plan is enough).

SENTRY_DSN turns it on; without it every function here does nothing. Privacy: no message text, prompts, replies or
request bodies are sent. Spans carry stage names, model ids, purposes and timings; events carry the session, turn and
policy hash so a trace can be matched to the audit log. Errors that reach a user as a polite reply are recorded as
breadcrumbs only, never as user-facing text."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from yachat.logging import get_logger

log = get_logger("tracing")
_on = False


def _scrub(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    """Drop anything that could hold what people typed: request bodies, cookies, query strings, local variables."""
    req = event.get("request")
    if isinstance(req, dict):
        for k in ("data", "cookies", "query_string"):
            req.pop(k, None)
        headers = req.get("headers")
        if isinstance(headers, dict):
            req["headers"] = {k: v for k, v in headers.items() if k.lower() not in ("authorization", "cookie", "x-api-key")}
    for exc in (event.get("exception") or {}).get("values") or []:
        for frame in (exc.get("stacktrace") or {}).get("frames") or []:
            frame.pop("vars", None)
    return event


def init(service: str) -> bool:
    """Call once at process start. True when Sentry is on."""
    global _on
    dsn = os.getenv("SENTRY_DSN")
    if not dsn or _on:
        return _on
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv("SENTRY_ENVIRONMENT") or "production",
        release=os.getenv("SENTRY_RELEASE") or None,
        server_name=service,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE") or 0.1),
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        before_send=_scrub,  # type: ignore[arg-type]
        before_send_transaction=_scrub,  # type: ignore[arg-type]
    )
    sentry_sdk.set_tag("service", service)
    _on = True
    log.info("sentry on", service=service, traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE") or 0.1))
    return True


@contextmanager
def span(op: str, name: str, **data: Any) -> Iterator[None]:
    """A child span of the current request's trace (a pipeline stage, a model call). No-op when Sentry is off."""
    if not _on:
        yield
        return
    import sentry_sdk

    with sentry_sdk.start_span(op=op, name=name) as s:
        for k, v in data.items():
            s.set_data(k, v)
        yield


def tag_turn(session_id: str, turn_id: str, policy_hash: str, agent: str, tenant: str) -> None:
    """Identifiers only, so an error or slow trace can be found in the audit log."""
    if not _on:
        return
    import sentry_sdk

    scope = sentry_sdk.get_current_scope()
    scope.set_tag("agent", agent)
    scope.set_tag("tenant", tenant)
    scope.set_tag("policy_hash", policy_hash)
    scope.set_context("turn", {"session_id": session_id, "turn_id": turn_id})

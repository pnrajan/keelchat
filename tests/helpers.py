from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import yaml

from keelchat.policy.compiler import build
from keelchat.runtime.context import InboundMessage
from keelchat.runtime.llm import LLMClient, LLMRequest, ModelConfig
from keelchat.runtime.llm_factory import llm_from_env
from keelchat.runtime.runtime import Runtime, TurnResult
from keelchat.state.models import AuditEvent
from keelchat.state.store import InMemoryAuditStore
from keelchat.testing.fakes import FakeTools

AGENT = "hiring-agent"


def mutate(policy_yaml: str, fn: Any) -> str:
    data = yaml.safe_load(policy_yaml)
    fn(data)
    return yaml.safe_dump(data, sort_keys=False)


class RecordingLLM(LLMClient):
    """The configured real model, with every request recorded so tests can assert on what was asked (never on canned answers)."""

    def __init__(self, inner: LLMClient | None = None):
        self.inner = inner or llm_from_env()
        self.calls: list[LLMRequest] = []

    async def complete(self, req: LLMRequest) -> str:
        self.calls.append(req)
        return await self.inner.complete(req)

    async def structured(self, req: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(req)
        return await self.inner.structured(req, schema)

    def calls_for(self, purpose: str) -> list[LLMRequest]:
        return [c for c in self.calls if c.purpose == purpose]


async def make_runtime(policy_yaml: str, llm: RecordingLLM | None = None, tools: FakeTools | None = None, **kw: Any) -> tuple[Runtime, FakeTools]:
    tools = tools or FakeTools()
    kw.setdefault("models", ModelConfig.from_env())
    rt = Runtime(llm or RecordingLLM(), tools.impls(), **kw)
    await rt.register_policy(build(policy_yaml))
    return rt, tools


async def say(rt: Runtime, session_id: str, text: str, mid: str | None = None, channel: str = "web", user: str = "u1", action: str | None = None) -> TurnResult:
    return await rt.turn(session_id, InboundMessage(text=text, channel=channel, user_id=user, channel_message_id=mid, action=action))


async def reach_form(rt: Runtime, session_id: str, pick: str = "1", query: str = "backend engineer in austin") -> TurnResult:
    """Greet → consent → search → pick → first form question."""
    await say(rt, session_id, "hello")
    await say(rt, session_id, "yes")
    await say(rt, session_id, query)
    return await say(rt, session_id, pick)


async def fill_form(rt: Runtime, session_id: str) -> TurnResult:
    await say(rt, session_id, "Jane Doe")
    return await say(rt, session_id, "123 Main St, Austin TX 78701")


async def audit_window(events: list[AuditEvent], tenant: str, since: datetime = datetime(1970, 1, 1, tzinfo=UTC)) -> Any:
    """`events` loaded the way the dashboards read them: one tenant's window in a DuckDB table `audit` (close it)."""
    from keelchat.runtime.audit import audit_db

    store = InMemoryAuditStore()
    await store.append(events)
    return await audit_db(store, tenant, since)


class ScriptedLLM(RecordingLLM):
    """Deterministic where it matters: a purpose with a script answers from it in order (a value that is an exception
    is raised); every other request goes to the real model. For control-flow and failure tests of the runtime, where a
    canned model reply is the point and the real model would only add noise."""

    def __init__(self, scripts: dict[str, list[Any]], inner: LLMClient | None = None):
        super().__init__(inner)
        self.scripts = {k: list(v) for k, v in scripts.items()}

    def _next(self, req: LLMRequest) -> Any:
        script = self.scripts.get(req.purpose)
        if not script:
            return None
        value = script.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

    async def complete(self, req: LLMRequest) -> str:
        if (value := self._next(req)) is not None:
            self.calls.append(req)
            return str(value)
        return await super().complete(req)

    async def structured(self, req: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        if (value := self._next(req)) is not None:
            self.calls.append(req)
            return dict(value)
        return await super().structured(req, schema)

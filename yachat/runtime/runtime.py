"""Composition point: stores, LLM, tools and the three stages. Lock, dedupe, policy pinning, async memory."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from yachat.channels import CHANNELS
from yachat.datasets.models import Dataset
from yachat.datasets.store import DatasetStore, InMemoryDatasetStore
from yachat.logging import Metrics, bound, get_logger
from yachat.policy.compiler import CompiledPolicy, build
from yachat.policy.errors import PolicyValidationError
from yachat.registry.core import REGISTRY
from yachat.runtime.audit import events_for, llm_events
from yachat.runtime.context import ChannelMessage, InboundMessage, OutboundMessage, Services, StageTrace, TurnContext
from yachat.runtime.decide.intent_detector import IntentDetector
from yachat.runtime.decide.orchestrator import Orchestrator
from yachat.runtime.decide.tool_gateway import ActionRunner, ToolGateway
from yachat.runtime.llm import LLMClient, ModelConfig, small_only
from yachat.runtime.pipeline import CompositeStage, Pipeline
from yachat.runtime.respond.channel import ChannelStage
from yachat.runtime.respond.guardrails import OutputGuardrails
from yachat.runtime.respond.response_generator import ResponseGenerator
from yachat.runtime.respond.rewriter import Rewriter
from yachat.runtime.understand.stage import UnderstandStage
from yachat.runtime.usage import LIMIT_REACHED, METER, Usage, charge_usd
from yachat.state.memory_writer import MemorySnapshot, MemoryWriter
from yachat.state.models import PolicyVersion, SessionState
from yachat.state.store import (
    AgentVersionStore,
    AuditStore,
    BudgetStore,
    HandoffStore,
    InMemoryAgentVersionStore,
    InMemoryAuditStore,
    InMemoryBudgetStore,
    InMemoryHandoffStore,
    InMemoryMemoryStore,
    InMemoryPolicyStore,
    InMemoryRateLimiter,
    InMemoryResponseStore,
    InMemorySessionStore,
    InMemoryTemplateStore,
    InMemoryTurnLocks,
    MemoryStore,
    Plan,
    PolicyStore,
    RateLimiter,
    ResponseStore,
    SessionStore,
    TemplateStore,
    TurnLocks,
    VersionConflict,
)
from yachat.tracing import tag_turn

log = get_logger("runtime")
ToolImpl = Callable[..., Awaitable[Any]]


class RuntimeFailure(Exception):
    pass


class NotFound(RuntimeFailure):
    pass


class PolicyRetired(RuntimeFailure):
    pass


@dataclass
class TurnResult:
    turn_id: str
    outbound: OutboundMessage
    rendered: list[ChannelMessage]
    trace: list[StageTrace]
    state: SessionState
    errors: list[str]
    llm_calls: int
    tool_calls: int
    duplicate: bool = False
    route: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_id": self.turn_id,
            "outbound": asdict(self.outbound),
            "rendered": [asdict(m) for m in self.rendered],
            "trace": [asdict(t) for t in self.trace],
            "errors": self.errors,
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "duplicate": self.duplicate,
            "route": self.route,
        }


class Runtime:
    def __init__(
        self,
        llm: LLMClient,
        tool_impls: dict[str, ToolImpl] | None = None,
        *,
        models: ModelConfig | None = None,
        policies: PolicyStore | None = None,
        sessions: SessionStore | None = None,
        memory: MemoryStore | None = None,
        datasets: DatasetStore | None = None,
        versions: AgentVersionStore | None = None,
        templates: TemplateStore | None = None,
        audit: AuditStore | None = None,
        handoffs: HandoffStore | None = None,
        responses: ResponseStore | None = None,
        budgets: BudgetStore | None = None,
        limits: RateLimiter | None = None,
        locks: TurnLocks | None = None,
        metrics: Metrics | None = None,
        gateway_kwargs: dict[str, Any] | None = None,
    ):
        REGISTRY.discover()
        self.llm = llm
        self.models = models or ModelConfig()
        self.tool_impls = tool_impls or {}
        self.policies = policies or InMemoryPolicyStore()
        self.sessions = sessions or InMemorySessionStore()
        self.memory_store = memory or InMemoryMemoryStore()
        self.datasets = datasets or InMemoryDatasetStore()
        self.versions = versions or InMemoryAgentVersionStore()
        self.templates = templates or InMemoryTemplateStore()
        self.audit = audit or InMemoryAuditStore()
        self.handoffs = handoffs or InMemoryHandoffStore()
        self.responses = responses or InMemoryResponseStore()
        self.budgets = budgets or InMemoryBudgetStore()
        self.limits = limits or InMemoryRateLimiter()  # public-traffic rate limits, shared by servers on Postgres
        self.locks = locks or InMemoryTurnLocks()
        self.memory_writer = MemoryWriter(self.memory_store, self.llm, self.models)
        self.metrics = metrics or Metrics()
        self.gateway_kwargs = gateway_kwargs or {}
        self._tasks: set[asyncio.Task[Any]] = set()
        m = self.metrics
        self.pipeline = Pipeline(
            [
                UnderstandStage(m),
                CompositeStage("decide", [IntentDetector(), Orchestrator()], m),
                CompositeStage("respond", [Rewriter(), OutputGuardrails(), ResponseGenerator(), ChannelStage()], m, respects_short_circuit=False),
            ],
            metrics=m,
        )

    # ---------------------------------------------------------------- policies & sessions
    async def register_policy(self, policy: CompiledPolicy) -> CompiledPolicy:
        await self.policies.put(policy)
        log.info("policy registered", agent=policy.agent, policy_hash=policy.hash)
        return policy

    async def save_version(self, policy: CompiledPolicy, yaml: str, note: str = "", activate: bool = True) -> tuple[PolicyVersion, bool]:
        """Store a valid build as the agent's next version (the existing one when the build is unchanged)."""
        v, created = await self.versions.save(policy.tenant, policy.agent, policy.hash, yaml, note)
        await self.register_policy(policy)
        if activate:
            await self._activate(v, policy)
        log.info("policy version saved", agent=policy.agent, version=v.version, created=created, policy_hash=policy.hash)
        return v, created

    async def activate_version(self, tenant: str, agent: str, version: int) -> PolicyVersion:
        """New sessions of the agent start on this version; existing sessions keep theirs (also a rollback)."""
        v = await self.versions.get(tenant, agent, version)
        if v is None:
            raise NotFound(f"version {version} of agent {agent!r}")
        await self._activate(v, await self._compiled(v))
        return v

    async def load_saved_policies(self) -> int:
        """Rebuild and register every agent's active version (compiled policies live in memory). A version that no
        longer builds (a capability was removed) is logged and skipped."""
        n = 0
        for v in await self.versions.all_active():
            try:
                await self.policies.activate((await self._compiled(v)).hash)
                n += 1
            except PolicyValidationError as e:
                log.error("saved version no longer builds", agent=v.agent, version=v.version, errors=[x.path for x in e.errors])
        return n

    async def _compiled(self, v: PolicyVersion) -> CompiledPolicy:
        policy = await self.policies.get(v.hash)
        if policy is None:
            catalog = {d.name: d.kind for d in await self.datasets.list(v.tenant)}
            policy = await self.register_policy(build(v.yaml, tenant=v.tenant, datasets=catalog))
            if policy.hash != v.hash:  # the registry or defaults changed since the save; the YAML is the source
                log.warning("saved version rebuilt with a new hash", agent=v.agent, version=v.version, saved=v.hash, policy_hash=policy.hash)
        return policy

    async def _for_new_session(self, agent: str, tenant: str) -> CompiledPolicy | None:
        """The policy new sessions start on: the agent's active saved version, read from the version store every replica
        shares (so an activation or rollback anywhere applies everywhere); for an agent never saved, the latest build."""
        v = await self.versions.active(tenant, agent)
        if v is not None:
            try:
                return await self._compiled(v)
            except PolicyValidationError as e:  # logged; the last build that worked keeps serving
                log.error("active version no longer builds", agent=agent, version=v.version, errors=[x.path for x in e.errors])
        return await self.policies.latest(agent, tenant)

    async def _pinned(self, state: SessionState) -> CompiledPolicy | None:
        """The policy a session is pinned to; after a restart or on another replica it is rebuilt from the saved version
        with that hash (law 7: a conversation keeps the policy it started on)."""
        policy = await self.policies.get(state.policy_hash)
        if policy is not None or await self.policies.is_retired(state.policy_hash):
            return policy
        for v in await self.versions.versions(state.tenant, state.agent):
            if v.hash == state.policy_hash:
                try:
                    return await self._compiled(v)
                except PolicyValidationError as e:
                    log.error("pinned version no longer builds", agent=v.agent, version=v.version, errors=[x.path for x in e.errors])
                    return None
        return None

    async def _activate(self, v: PolicyVersion, policy: CompiledPolicy) -> None:
        await self.versions.activate(v.tenant, v.agent, v.version)
        await self.policies.activate(policy.hash)

    async def start_session(
        self,
        agent: str,
        user_id: str,
        channel: str = "web",
        policy_hash: str | None = None,
        *,
        tenant: str = "default",
        resume: bool = True,
        now: datetime | None = None,
    ) -> SessionState:
        """Resume the user's unfinished session for this agent if its last turn is within the TTL; otherwise start a new one."""
        policy = await self.policies.get(policy_hash) if policy_hash else await self._for_new_session(agent, tenant)
        if policy is None or policy.tenant != tenant or policy.agent != agent:
            if policy_hash and await self.policies.latest(agent, tenant) is not None:
                raise PolicyRetired(f"policy {policy_hash} for agent {agent!r} is retired or unknown")
            raise NotFound(f"no policy for agent {agent!r}")
        if channel not in CHANNELS:
            raise RuntimeFailure(f"unknown channel {channel!r}")
        now = now or datetime.now(UTC)
        if resume:
            open_ = await self.sessions.find_open(user_id, agent, tenant)
            if open_ is not None and now - open_.last_turn_at <= timedelta(days=policy.defaults.session_ttl_days):
                log.info("session resumed", session_id=open_.id, age_days=round((now - open_.last_turn_at).total_seconds() / 86400, 1))
                return open_
        state = SessionState(
            user_id=user_id,
            agent=agent,
            tenant=tenant,
            policy_hash=policy.hash,
            channel=channel,
            language=policy.fallback_language,
            created_at=now,
            last_turn_at=now,
        )
        return await self.sessions.save(state)

    async def label_session(self, session_id: str, segment: dict[str, str]) -> SessionState:
        """Labels a conversation's survey results are split by (campaign, channel, product, first or repeat buyer). Set
        once, when the conversation starts from an invite; labels already there are kept."""
        async with self.locks.hold(session_id):
            st = await self.session_state(session_id)
            current = st.data.setdefault("segment", {})
            for k, v in segment.items():
                if v and k not in current:
                    current[str(k)[:40]] = str(v)[:80]
            return await self.sessions.save(st)

    async def prefill_answer(self, session_id: str, key: str, value: Any) -> SessionState:
        """Start a survey with one answer already given: a score clicked in an email. Clicking it is the person choosing
        to answer the survey, so the consent step counts as done and the survey goes on from its next question. It is
        not a yes to being remembered: consent stays unset, so the memory writer withholds sensitive items (ordinary
        policy-listed items need no yes). Only a survey question of this session's policy can be filled, once, before
        any turn."""
        from yachat.capabilities.nodes.survey_answers import survey_questions_of

        async with self.locks.hold(session_id):
            st = await self.session_state(session_id)
            policy = await self.policies.get(st.policy_hash)
            questions = {q.key for q in survey_questions_of(policy.steps if policy else [])}
            if key not in questions or st.turns or key in st.fields:
                return st
            st.fields[key] = value
            st.data.setdefault("survey", {}).setdefault("capture", {})[key] = "clicked"
            if policy is not None:
                from yachat.state.flow import StepGraph

                graph = StepGraph(policy)
                graph.init(st)
                for step in policy.steps:
                    if step.node == "greet_consent":
                        st.outputs["consent"] = True
                        graph.complete(st, step.name)
            return await self.sessions.save(st)

    async def session_state(self, session_id: str) -> SessionState:
        st = await self.sessions.load(session_id)
        if st is None:
            raise NotFound(f"session {session_id!r}")
        return st

    async def _gateway(self, policy: CompiledPolicy, state: SessionState) -> ToolGateway:
        resolved: dict[str, Dataset] = {}
        for name in policy.datasets:  # read every turn: a re-registered dataset's new sources apply at once
            if (ds := await self.datasets.get(policy.tenant, name)) is not None:
                resolved[name] = ds
        return ToolGateway(set(policy.allowed_tools), dict(self.tool_impls), tenant=policy.tenant, datasets=resolved, **self.gateway_kwargs)

    # ---------------------------------------------------------------- turns
    async def turn_from_payload(self, session_id: str, payload: dict[str, Any]) -> TurnResult:
        """Channel entry point: compliance (e.g. SMS STOP) is handled by the channel before the pipeline."""
        state = await self.session_state(session_id)
        channel = CHANNELS[state.channel]
        stop = channel.compliance(payload)
        if stop is not None:
            log.info("channel compliance", channel=state.channel, kind=stop.payload.get("compliance"))
            return TurnResult("compliance", OutboundMessage(stop.text, language=state.language), [stop], [], state, [], 0, 0)
        return await self.turn(session_id, channel.normalize(payload))

    async def turn(self, session_id: str, inbound: InboundMessage, *, now: datetime | None = None) -> TurnResult:
        state = await self.session_state(session_id)
        policy = await self._pinned(state)
        if policy is None:
            raise PolicyRetired(f"policy {state.policy_hash} pinned by session {session_id} is no longer available")
        async with self.locks.hold(session_id):  # concurrent turns on one session run one at a time, across servers too
            mid = inbound.channel_message_id
            if mid:
                cached = await self.sessions.seen(session_id, mid)
                if cached is not None:
                    log.info("duplicate message dropped", has_channel_message_id=True)
                    return TurnResult(
                        cached["turn_id"],
                        OutboundMessage(**cached["outbound"]),
                        [ChannelMessage(**x) for x in cached["rendered"]],
                        [],
                        await self.session_state(session_id),
                        [],
                        0,
                        0,
                        duplicate=True,
                    )
            state = await self.session_state(session_id)  # fresh copy under the lock
            state.last_turn_at = now or datetime.now(UTC)
            result = await self._run_turn(state, policy, inbound)
            if mid:
                await self.sessions.mark_seen(
                    session_id,
                    mid,
                    {"turn_id": result.turn_id, "outbound": asdict(result.outbound), "rendered": [asdict(x) for x in result.rendered]},
                )
            return result

    async def _run_turn(self, state: SessionState, policy: CompiledPolicy, inbound: InboundMessage) -> TurnResult:
        gateway = await self._gateway(policy, state)
        models = self.models  # the policy pins model names per tier at build; the runtime honours the pins
        if policy.models:
            models = replace(self.models, classify=policy.models.get("small", self.models.classify), generate=policy.models.get("large", self.models.generate))
        plan = await self.budgets.get(policy.tenant)
        if plan is not None and plan.small_models:  # the plan allows small models only, whatever the policy pins
            models = small_only(models)
        services = Services(
            self.llm,
            models,
            gateway,
            self.metrics,
            actions=ActionRunner(dict(self.tool_impls)),
            memory=self.memory_store,
            handoffs=self.handoffs,
            responses=self.responses,
        )
        before, t0 = state.model_copy(deep=True), time.perf_counter()
        ctx = TurnContext(state=state, policy=policy, inbound=inbound, services=services)
        meter: list[Usage] = []
        token = METER.set(meter)
        try:
            return await self._metered_turn(ctx, before, t0, meter, plan)
        finally:
            METER.reset(token)

    async def _metered_turn(self, ctx: TurnContext, before: SessionState, t0: float, meter: list[Usage], plan: Plan | None) -> TurnResult:
        state, policy = ctx.state, ctx.policy
        tag_turn(state.id, ctx.turn_id, policy.hash, policy.agent, policy.tenant)
        with bound(session_id=state.id, turn_id=ctx.turn_id, policy_hash=policy.hash, stage="understand"):
            limited = plan is not None and plan.exhausted()
            if plan is not None and limited:  # checked in code before any model call
                ctx.short_circuit_with(LIMIT_REACHED, note="usage limit reached")
                log.info("usage limit reached", tenant=policy.tenant, turns=plan.turns, limit_turns=plan.limit_turns, spent_usd=round(plan.spent_usd, 6))
            ctx = await self.pipeline.run(ctx)
            try:
                ctx.state = await self.sessions.save(ctx.state)
            except VersionConflict as e:
                log.error("version conflict", error=str(e))
                raise
            await self._record(ctx, before, (time.perf_counter() - t0) * 1000, list(meter))
            await self.charge(policy.tenant, meter, turns=0 if limited else 1)
            if not ctx.input_flagged and not ctx.short_circuit:
                snap = MemorySnapshot(
                    user_id=state.user_id,
                    policy=policy,
                    turn_id=ctx.turn_id,
                    user_text=ctx.text,
                    validated_answers=dict(ctx.validated_answers),
                    consent=bool(ctx.state.consent),
                    declined=ctx.state.consent is False,
                    flagged=ctx.input_flagged,
                )
                task = asyncio.create_task(self._write_memory(snap, state.id))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            log.info("turn done", llm_calls=ctx.llm_calls, tool_calls=ctx.tool_calls, route=ctx.route, exit_reason=ctx.exit_reason)
        assert ctx.outbound is not None
        return TurnResult(ctx.turn_id, ctx.outbound, ctx.rendered, ctx.trace, ctx.state, ctx.errors, ctx.llm_calls, ctx.tool_calls, route=ctx.route)

    async def _record(self, ctx: TurnContext, before: SessionState, ms: float, calls: list[Usage]) -> None:
        """Store the turn's decisions. A failed write is logged and counted; it never fails the user's turn."""
        try:
            await self.audit.append(events_for(ctx, before, ms) + llm_events(ctx.policy, ctx.state.id, ctx.turn_id, calls))
        except Exception as e:  # the reply is already decided and saved
            self.metrics.incr("audit_write_failed")
            log.error("audit write failed", error=f"{e.__class__.__name__}: {e}")

    async def charge(self, tenant: str, calls: list[Usage], turns: int = 0) -> None:
        """Add the calls' cost (and answered turns) to the tenant's plan usage. A failed write is logged; it never fails the
        user's turn."""
        if not calls and not turns:
            return
        try:
            await self.budgets.charge(tenant, charge_usd(calls), turns)
        except Exception as e:
            log.error("budget charge failed", error=f"{e.__class__.__name__}: {e}")

    async def _write_memory(self, snap: MemorySnapshot, session_id: str) -> None:
        meter: list[Usage] = []
        METER.set(meter)  # this task's own context: its model calls are billed to the turn that started it
        with bound(session_id=session_id, turn_id=snap.turn_id, policy_hash=snap.policy.hash, stage="memory_writer"):
            try:
                await self.memory_writer.write(snap)
            except Exception as e:  # best effort; never affects the reply
                log.error("memory writer failed", error=f"{e.__class__.__name__}: {e}")
            await self.charge(snap.policy.tenant, meter)
            try:
                await self.audit.append(llm_events(snap.policy, session_id, snap.turn_id, meter))
            except Exception as e:  # never affects the reply
                self.metrics.incr("audit_write_failed")
                log.error("audit write failed", error=f"{e.__class__.__name__}: {e}")

    async def drain(self) -> None:
        """Wait for background memory writes (tests, shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

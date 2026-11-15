from __future__ import annotations

import sys

import pytest

from keelchat.capabilities.tools import ToolError
from keelchat.policy.compiler import build
from keelchat.registry.contract import ContractViolation, all_registered, check_capability, check_tool
from keelchat.registry.core import REGISTRY, ToolSpec, tool
from keelchat.registry.discovery import discover_packages, packages_from_env
from keelchat.registry.mcp import register_mcp_servers, servers_from_env
from keelchat.runtime.decide.tool_gateway import ToolGateway
from keelchat.testing.fakes import FakeTools
from tests.conftest import ROOT
from tests.helpers import make_runtime, mutate


@pytest.mark.parametrize("kind,name", all_registered(), ids=lambda x: str(x))
async def test_every_registered_capability_passes_contract(kind, name):
    impl = FakeTools().impls().get(name) if kind in ("tool", "action") else None
    await check_capability(kind, name, impl)


async def test_entry_point_tool_registered_usable_and_allowed_only_by_policy(policy_yaml):
    assert REGISTRY.has_tool("salary_lookup") and REGISTRY.tool("salary_lookup").description.startswith("Median salary")
    r = await ToolGateway({"salary_lookup"}).call("salary_lookup", {"title": "nurse"})
    assert r.ok and r.data["median"] == "$120k"
    denied = await ToolGateway(set()).call("salary_lookup", {"title": "nurse"})
    assert not denied.ok and denied.error_kind == "not_allowed"
    rt, _ = await make_runtime(mutate(policy_yaml, lambda d: d["tools"].append("salary_lookup")))
    s = await rt.start_session("hiring-agent", "u1")
    assert "salary_lookup" in (await rt.policies.get(s.policy_hash)).allowed_tools


async def test_gateway_breaker_scrub_and_args():
    failing = FakeTools(fail={"job_search"})
    gw = ToolGateway({"job_search"}, failing.impls(), retry_backoff_s=0.0, breaker_threshold=2, breaker_cooldown_s=60, sensitive_values={"woman"})
    await gw.call("job_search", {"role": "woman", "location": "x"})
    assert failing.calls["job_search"][0]["role"] == "[withheld]"
    await gw.call("job_search", {"role": "x"})
    r = await gw.call("job_search", {"role": "x"})
    assert r.error_kind == "circuit_open" and len(failing.calls["job_search"]) == 4 and 'trusted="false"' in r.as_prompt_data()
    assert (await ToolGateway({"job_search"}, failing.impls()).call("job_search", {"nope": 1})).error_kind == "invalid_args"


async def test_contract_rejects_bad_tool():
    async def leaky(x: str) -> str:
        import socket

        socket.socket()
        return x

    spec = ToolSpec(name="leaky", fn=leaky, input_model=REGISTRY.tool("knowledge_search").input_model, output_schema={"type": "string"})
    with pytest.raises(ContractViolation):
        await check_tool(spec, leaky)


@tool("flaky_probe", retries=2, timeout_s=1.0)
async def flaky_probe(key: str) -> dict[str, str]:
    """Always fails upstream; exists to prove the gateway honours `retries`."""
    raise ToolError("flaky_probe", "upstream", "down")


async def test_tool_retries_come_from_the_spec():
    calls = []

    async def impl(key: str) -> dict[str, str]:
        calls.append(key)
        raise ToolError("flaky_probe", "upstream", "down")

    r = await ToolGateway({"flaky_probe"}, {"flaky_probe": impl}, retry_backoff_s=0.0).call("flaky_probe", {"key": "k"})
    assert not r.ok and r.attempts == 3 and len(calls) == 3
    assert REGISTRY.manifest().versions()["tool:flaky_probe"] and REGISTRY.tool("flaky_probe").retries == 2


def test_capability_packages_from_env(tmp_path, monkeypatch):
    pkg = tmp_path / "acme_caps"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from keelchat.registry.core import tool\n\n@tool('acme_ping')\nasync def acme_ping(x: str) -> str:\n    'Ping.'\n    return x\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("KEELCHAT_CAPABILITY_PACKAGES", "acme_caps, nope_missing")
    assert packages_from_env() == ["acme_caps", "nope_missing"]
    assert discover_packages(packages_from_env()) == ["acme_caps"]  # the missing one is logged, not fatal
    assert REGISTRY.has_tool("acme_ping")


async def test_mcp_server_tools_are_registered_namespaced_and_run_through_the_gateway(monkeypatch):
    target = f"{sys.executable} {ROOT / 'tests' / 'mcp_server.py'}"
    monkeypatch.setenv("KEELCHAT_MCP_SERVERS", f"tm={target}, down=python -c 'import sys; sys.exit(1)'")
    assert servers_from_env() == {"tm": target, "down": "python -c 'import sys; sys.exit(1)'"}
    registered = register_mcp_servers(REGISTRY)
    assert registered == ["tm.lookup_plan", "tm.archive_plan"]  # the unreachable server is skipped, not fatal
    spec = REGISTRY.tool("tm.lookup_plan")
    assert spec.source == "mcp" and spec.description == "Development plan for an employee." and list(spec.input_model.model_fields) == ["employee_id", "region"]
    assert spec.side_effects == "none" and spec.retries == 1  # the server said readOnlyHint
    unhinted = REGISTRY.tool("tm.archive_plan")
    assert unhinted.side_effects == "write" and unhinted.retries == 0  # no hint: MCP's default is "may have side effects"
    r = await ToolGateway({"tm.lookup_plan"}).call("tm.lookup_plan", {"employee_id": "e7"})
    assert (
        r.ok and r.data["plan"] == "Data Analyst track" and r.data["region"] == "us" and 'trusted="false"' in r.as_prompt_data()
    )  # omitted optional arg: server default, not null
    assert (await ToolGateway({"tm.lookup_plan"}).call("tm.lookup_plan", {"nope": 1})).error_kind == "invalid_args"
    monkeypatch.setenv("KEELCHAT_MCP_SERVERS", "missing-equals-sign")
    assert register_mcp_servers(REGISTRY) == []  # bad configuration is logged, never fatal
    assert build(MINIMAL_MCP).allowed_tools == ["tm.lookup_plan"]

    async def fake(employee_id: str) -> dict[str, str]:
        return {"employee_id": employee_id}

    await check_capability("tool", "tm.lookup_plan", fake)  # the same contract as every other tool, with a fake backend


MINIMAL_MCP = """
agent: mcp-agent
charter: test
tools: [tm.lookup_plan]
"""


def test_a_write_tool_is_not_retried_unless_asked():
    """A retried write can do it twice (book twice, submit twice): `side_effects="write"` defaults to no retries, as
    tools registered over HTTP and MCP already do. An explicit `retries` is kept."""
    from keelchat.registry.core import REGISTRY, tool

    @tool("audit_write_tool", side_effects="write")
    async def write_once(x: str) -> dict[str, str]:
        """Writes once."""
        return {"x": x}

    @tool("audit_write_tool_retried", side_effects="write", retries=2)
    async def write_retried(x: str) -> dict[str, str]:
        """Safe to repeat."""
        return {"x": x}

    @tool("audit_read_tool")
    async def read(x: str) -> dict[str, str]:
        """Reads."""
        return {"x": x}

    try:
        assert REGISTRY.tool("audit_write_tool").retries == 0
        assert REGISTRY.tool("audit_write_tool_retried").retries == 2
        assert REGISTRY.tool("audit_read_tool").retries == 1
    finally:
        for n in ("audit_write_tool", "audit_write_tool_retried", "audit_read_tool"):
            REGISTRY._tools.pop(n, None)


async def test_the_breaker_is_shared_by_a_tenants_turns_and_kept_from_other_tenants(policy_yaml):
    """A gateway is built per turn; the breaker behind it is per tenant and per process, so a backend that keeps
    failing stays open across turns and sessions, and one tenant's outage never opens another tenant's circuit."""
    from keelchat.state.models import SessionState

    failing = FakeTools(fail={"job_search"})
    rt, _ = await make_runtime(policy_yaml, tools=failing, gateway_kwargs={"retry_backoff_s": 0.0, "breaker_threshold": 2, "breaker_cooldown_s": 60})
    p = await rt.policies.latest("hiring-agent", "default")
    assert p is not None
    other = build(policy_yaml, tenant="other")
    await rt.register_policy(other)
    st = SessionState(user_id="u", agent=p.agent, policy_hash=p.hash, channel="web")
    for _ in range(2):  # two turns, two gateways, one breaker
        await (await rt._gateway(p, st)).call("job_search", {"role": "x"})
    r = await (await rt._gateway(p, st)).call("job_search", {"role": "x"})
    assert r.error_kind == "circuit_open" and len(failing.calls["job_search"]) == 4
    r2 = await (await rt._gateway(other, SessionState(user_id="u", agent=other.agent, policy_hash=other.hash, channel="web"))).call("job_search", {"role": "x"})
    assert r2.error_kind != "circuit_open"  # the other tenant's circuit is its own


async def test_a_huge_tool_result_is_cut_before_it_enters_a_prompt():
    from keelchat.runtime.decide.tool_gateway import PROMPT_DATA_MAX_CHARS, ToolResult

    big = ToolResult("t", True, data={"rows": ["x" * 1000] * 100})
    text = big.as_prompt_data()
    assert len(text) < PROMPT_DATA_MAX_CHARS + 200 and "truncated" in text and 'trusted="false"' in text
    assert "truncated" not in ToolResult("t", True, data={"ok": 1}).as_prompt_data()


def test_a_failed_tool_enters_a_prompt_as_unavailable_not_by_its_error_kind():
    """The planner must not be able to tell the user "configuration error" or "circuit open": the kind stays in the
    logs; a code-written fix (a correctable call) is still shown."""
    from keelchat.runtime.decide.tool_gateway import ToolResult

    down = ToolResult("job_search", False, error="JOB_SEARCH_URL is not configured", error_kind="config").as_prompt_data()
    assert "unavailable" in down and "config" not in down and "JOB_SEARCH_URL" not in down
    assert "circuit" not in ToolResult("job_search", False, error="circuit open", error_kind="circuit_open").as_prompt_data()
    fixable = ToolResult("job_search", False, error="1 error(s)", error_kind="invalid_args", fix="role is required").as_prompt_data()
    assert "role is required" in fixable and "invalid_args" not in fixable


async def test_a_contract_check_blocks_only_its_own_task(monkeypatch):
    """The socket guard is process-wide but raises only inside the task that asked for it: a tenant registration on a
    live server must not trip the request, database or model calls running beside it."""
    import asyncio
    import socket

    from keelchat.registry.contract import no_network

    seen: list[str] = []
    real = socket.socket

    async def bystander() -> None:
        await asyncio.sleep(0.01)
        s = socket.socket()  # a concurrent coroutine outside the block
        assert isinstance(s, socket.socket) and issubclass(socket.socket, real)  # still a socket type while guarded
        s.close()
        seen.append("ok")

    async def checked() -> None:
        with no_network():
            await asyncio.sleep(0.02)
            with pytest.raises(ContractViolation):
                socket.socket()
        seen.append("blocked")

    await asyncio.gather(checked(), bystander())
    assert sorted(seen) == ["blocked", "ok"] and socket.socket is not None
    s = socket.socket()  # restored after the last block
    s.close()


async def test_identity_arguments_come_from_the_server_not_the_caller():
    """A tool that takes `session_id`, `user_id`, `tenant` or `agent` gets the server's values: whatever a node or
    the model wrote there is replaced, so model-generated arguments cannot speak for another session or user."""
    seen: list[dict[str, object]] = []

    async def notify(reason: str, session_id: str, agent: str, **kw: object) -> dict[str, str]:
        seen.append({"reason": reason, "session_id": session_id, "agent": agent})
        return {"ticket_id": "t1"}

    ctx = {"tenant": "acme", "user_id": "u-real", "session_id": "ses-real", "agent": "hiring-agent"}
    gw = ToolGateway({"handoff_webhook"}, {"handoff_webhook": notify}, context=ctx)
    r = await gw.call("handoff_webhook", {"reason": "user_request", "session_id": "ses-forged", "agent": "other-agent", "fields": {}, "transcript": []})
    assert r.ok and seen == [{"reason": "user_request", "session_id": "ses-real", "agent": "hiring-agent"}]
    assert gw.calls[-1][1]["session_id"] == "ses-real"  # the audit sees the trusted value too
    plain = await ToolGateway({"job_search"}, FakeTools().impls(), context=ctx).call("job_search", {"role": "x"})
    assert plain.ok  # a tool without identity arguments is untouched

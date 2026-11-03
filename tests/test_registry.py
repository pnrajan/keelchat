from __future__ import annotations

import sys

import pytest

from tests.conftest import ROOT
from tests.helpers import make_runtime, mutate
from yachat.capabilities.tools import ToolError
from yachat.policy.compiler import build
from yachat.registry.contract import ContractViolation, all_registered, check_capability, check_tool
from yachat.registry.core import REGISTRY, ToolSpec, tool
from yachat.registry.discovery import discover_packages, packages_from_env
from yachat.registry.mcp import register_mcp_servers, servers_from_env
from yachat.runtime.decide.tool_gateway import ToolGateway
from yachat.testing.fakes import FakeTools


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
        "from yachat.registry.core import tool\n\n@tool('acme_ping')\nasync def acme_ping(x: str) -> str:\n    'Ping.'\n    return x\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("YACHAT_CAPABILITY_PACKAGES", "acme_caps, nope_missing")
    assert packages_from_env() == ["acme_caps", "nope_missing"]
    assert discover_packages(packages_from_env()) == ["acme_caps"]  # the missing one is logged, not fatal
    assert REGISTRY.has_tool("acme_ping")


async def test_mcp_server_tools_are_registered_namespaced_and_run_through_the_gateway(monkeypatch):
    target = f"{sys.executable} {ROOT / 'tests' / 'mcp_server.py'}"
    monkeypatch.setenv("YACHAT_MCP_SERVERS", f"tm={target}, down=python -c 'import sys; sys.exit(1)'")
    assert servers_from_env() == {"tm": target, "down": "python -c 'import sys; sys.exit(1)'"}
    registered = register_mcp_servers(REGISTRY)
    assert registered == ["tm.lookup_plan"]  # the unreachable server is skipped, not fatal
    spec = REGISTRY.tool("tm.lookup_plan")
    assert spec.source == "mcp" and spec.description == "Development plan for an employee." and list(spec.input_model.model_fields) == ["employee_id", "region"]
    r = await ToolGateway({"tm.lookup_plan"}).call("tm.lookup_plan", {"employee_id": "e7"})
    assert (
        r.ok and r.data["plan"] == "Data Analyst track" and r.data["region"] == "us" and 'trusted="false"' in r.as_prompt_data()
    )  # omitted optional arg: server default, not null
    assert (await ToolGateway({"tm.lookup_plan"}).call("tm.lookup_plan", {"nope": 1})).error_kind == "invalid_args"
    monkeypatch.setenv("YACHAT_MCP_SERVERS", "missing-equals-sign")
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

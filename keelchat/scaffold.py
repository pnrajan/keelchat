"""`keelchat new`: starter files that build and pass the contract suite as written, to edit from there."""

from __future__ import annotations

import re
from pathlib import Path

AGENT = """\
# {name}: a starter agent. Every key: docs/WRITING_AN_AGENT.md. Check it with `keelchat build {file}`.
agent: {name}
charter: >
  Help customers with one clear job. Friendly, concise.
  Out of scope: anything unrelated to that job.
languages: [en]
flow:
  - greet_consent
  - form_fill:
      fields:
        - name: request
          ask: What can I help you with today?
          expect: A short description of what they need
guardrails: {{input: [prompt_injection], output: [pii]}}
limits: {{reasks: 3, digressions: 3}}
handoff: {{when: [user_request, max_reasks]}}
"""

PACKAGE_INIT = '''\
"""{pkg}: a keelchat capability package. Load it with KEELCHAT_CAPABILITY_PACKAGES={pkg} (its parent on PYTHONPATH):
every module here is imported at startup and the decorators register what they define."""
'''

TOOLS = '''\
"""A read-only tool. The type hints become its schema; the docstring is what the model reads."""

from __future__ import annotations

from keelchat import tool

ORDERS = {{"ORD-1": "shipped", "ORD-2": "processing"}}  # replace with a call to your system


@tool("{pkg}_order_status", timeout_s=3.0, retries=1)
async def order_status(order_id: str) -> dict[str, str]:
    """The status of an order, by its id (e.g. ORD-1); "not_found" for an unknown id."""
    # when your system is down or misconfigured: raise keelchat.ToolError(name, "upstream" or "config", message)
    return {{"order_id": order_id, "status": ORDERS.get(order_id.strip().upper(), "not_found")}}
'''

STEPS = '''\
"""A flow step: asks for an order id, looks it up through the Tool Gateway, and completes with the status.

The step decides what happens in code; the model only phrases the reply from `goal`."""

from __future__ import annotations

from keelchat import CapabilityConfig, Node, NodeResult, TurnContext, answers_for, node

ASK = ("order_id", "What is your order number?", "An order id such as ORD-1")


class OrderLookupConfig(CapabilityConfig):
    #: said when the order is found; {{status}} is filled in
    found: str = "Your order is {{status}}."


@node("{pkg}_order_lookup", tools=["{pkg}_order_status"], produces=["order_status"])
class OrderLookup(Node[OrderLookupConfig]):
    """Looks up the status of the customer's order and tells them."""

    config_model = OrderLookupConfig

    async def act(self, ctx: TurnContext, config: OrderLookupConfig) -> NodeResult:
        order_id = answers_for(ctx, "{pkg}_order_lookup").get("order_id")
        if not order_id:
            return NodeResult(ASK[1], goal="Ask for their order number.", ask=ASK)
        res = await ctx.services.gateway.call("{pkg}_order_status", {{"order_id": order_id}})
        ctx.tool_calls += 1
        if not res.ok or not isinstance(res.data, dict) or res.data.get("status") == "not_found":
            return NodeResult("I couldn't find that order. Please check the number.", goal="Say the order wasn't found and ask again.", ask=ASK)
        text = config.found.format(status=res.data["status"])
        return NodeResult(text, goal=f"Tell them: {{text}}", outputs={{"order_status": res.data["status"]}}, complete=True)
'''

GUARDRAILS = '''\
"""An output guardrail: replies never promise a delivery date."""

from __future__ import annotations

import re

from keelchat import GuardContext, Guardrail, GuardResult, guardrail

PROMISE = re.compile(r"\\b(guarantee[ds]?|will arrive (on|by))\\b", re.IGNORECASE)


@guardrail("{pkg}_no_delivery_promises", stage="output")
class NoDeliveryPromises(Guardrail):
    """Replaces delivery promises with a neutral line."""

    async def check(self, text: str, ctx: GuardContext) -> GuardResult:
        if not PROMISE.search(text):
            return GuardResult()
        return GuardResult(flagged=True, reason="delivery promised", redacted="I can't promise a delivery date, but I can share the latest status.")
'''

PACKAGE_AGENT = """\
# {name}: uses the {pkg} package. Build it with:
#   PYTHONPATH=. KEELCHAT_CAPABILITY_PACKAGES={pkg} keelchat build {file}
agent: {name}
charter: >
  Tell customers the status of their order. Friendly, concise.
  Out of scope: anything unrelated to orders.
languages: [en]
flow:
  - greet_consent
  - {pkg}_order_lookup
guardrails: {{input: [prompt_injection], output: [pii, {pkg}_no_delivery_promises]}}
limits: {{reasks: 3, digressions: 3}}
handoff: {{when: [user_request, max_reasks]}}
"""

TEST = '''\
"""The contract suite for {pkg}: schemas, a call, the timeout, no sockets outside the gateway.

Run: PYTHONPATH=. KEELCHAT_CAPABILITY_PACKAGES={pkg} pytest test_{pkg}.py"""

from __future__ import annotations

import pytest
from keelchat import check_capability
from keelchat.registry.core import REGISTRY

import {pkg}.guardrails  # noqa: F401  (registers on import)
import {pkg}.steps  # noqa: F401
import {pkg}.tools  # noqa: F401


@pytest.mark.parametrize("kind,name", [("tool", "{pkg}_order_status"), ("node", "{pkg}_order_lookup"), ("guardrail", "{pkg}_no_delivery_promises")])
async def test_contract(kind: str, name: str) -> None:
    REGISTRY.discover()
    await check_capability(kind, name)
'''


class ScaffoldError(Exception):
    pass


def _write(files: dict[Path, str]) -> list[Path]:
    taken = [p for p in files if p.exists()]
    if taken:
        raise ScaffoldError("already exists, nothing written: " + ", ".join(str(p) for p in taken))
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return list(files)


def new_agent(name: str, out: Path) -> list[Path]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ScaffoldError(f"agent name {name!r}: use lowercase letters, digits and dashes (e.g. returns-desk)")
    path = out / f"{name}.yaml"
    return _write({path: AGENT.format(name=name, file=path)})


def new_package(pkg: str, out: Path) -> list[Path]:
    if not re.fullmatch(r"[a-z_][a-z0-9_]*", pkg):
        raise ScaffoldError(f"package name {pkg!r}: use a lowercase Python identifier (e.g. acme_caps)")
    agent = f"{pkg.replace('_', '-')}-agent"
    root = out / pkg
    return _write(
        {
            root / "__init__.py": PACKAGE_INIT.format(pkg=pkg),
            root / "tools.py": TOOLS.format(pkg=pkg),
            root / "steps.py": STEPS.format(pkg=pkg),
            root / "guardrails.py": GUARDRAILS.format(pkg=pkg),
            out / f"{agent}.yaml": PACKAGE_AGENT.format(name=agent, pkg=pkg, file=f"{agent}.yaml"),
            out / f"test_{pkg}.py": TEST.format(pkg=pkg),
        }
    )

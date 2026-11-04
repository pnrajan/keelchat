"""`yachat new` writes files that build and pass the contract suite; `from yachat import ...` is the extension API."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import yachat
from yachat.cli import main
from yachat.policy.compiler import build_file
from yachat.registry.contract import check_capability
from yachat.scaffold import ScaffoldError, new_agent, new_package


def test_top_level_names_are_the_real_objects() -> None:
    for name, module in yachat._EXPORTS.items():
        assert getattr(yachat, name) is getattr(importlib.import_module(module), name), name
    with pytest.raises(AttributeError):
        yachat.not_a_name  # noqa: B018


def test_new_agent_builds(tmp_path: Path) -> None:
    (path,) = new_agent("returns-desk", tmp_path)
    assert build_file(str(path)).agent == "returns-desk"


def test_new_refuses_to_overwrite_and_bad_names(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    new_agent("desk", tmp_path)
    with pytest.raises(ScaffoldError, match="already exists"):
        new_agent("desk", tmp_path)
    with pytest.raises(ScaffoldError, match="lowercase"):
        new_agent("Bad_Name", tmp_path)
    with pytest.raises(ScaffoldError, match="identifier"):
        new_package("acme-caps", tmp_path)
    assert main(["new", "agent", "desk", "--out", str(tmp_path)]) == 1
    assert "already exists" in capsys.readouterr().err


async def test_new_package_registers_passes_contract_and_builds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    new_package("scaffold_caps", tmp_path)
    monkeypatch.syspath_prepend(str(tmp_path))
    for mod in ("tools", "steps", "guardrails"):
        importlib.import_module(f"scaffold_caps.{mod}")
    await check_capability("tool", "scaffold_caps_order_status")
    await check_capability("node", "scaffold_caps_order_lookup")
    await check_capability("guardrail", "scaffold_caps_no_delivery_promises")
    assert build_file(str(tmp_path / "scaffold-caps-agent.yaml")).agent == "scaffold-caps-agent"

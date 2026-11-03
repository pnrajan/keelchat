from __future__ import annotations

from pathlib import Path

import pytest
from dotenv import load_dotenv

from yachat.registry.core import REGISTRY
from yachat.runtime.llm_factory import llm_from_env

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "hiring_agent.yaml"

load_dotenv(ROOT / ".env")  # tests run against the real model configured there (YACHAT_LLM + provider key)
try:
    llm_from_env()
except RuntimeError as e:
    raise pytest.UsageError(f"tests need a real model: {e}") from None


@pytest.fixture(scope="session", autouse=True)
def _registry() -> None:
    REGISTRY.discover()


@pytest.fixture(autouse=True)
def _no_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepEval judged metrics are an evals feature (`yachat evals run`), not part of the unit suite."""
    monkeypatch.delenv("EVALS_JUDGE_MODEL", raising=False)


@pytest.fixture
def policy_yaml() -> str:
    return EXAMPLE.read_text()

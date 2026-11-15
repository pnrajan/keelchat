from __future__ import annotations

from collections.abc import Generator
from pathlib import Path

import pytest
from dotenv import load_dotenv

from keelchat.registry.core import REGISTRY
from keelchat.runtime.llm_factory import llm_from_env

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "hiring_agent.yaml"

load_dotenv(ROOT / ".env")  # tests run against the real model configured there (KEELCHAT_LLM + provider key)
try:
    llm_from_env()
    MODEL_ERROR = ""
except RuntimeError as e:
    MODEL_ERROR = str(e)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--offline", action="store_true", help="skip the tests that need the model even when a key is set")


def pytest_configure(config: pytest.Config) -> None:
    """Without a model key, `pytest` runs the offline tests and skips the rest (a stranger's clone must not error out);
    `-m smoke` is the short real-model set, `-m "not smoke"` the rest."""
    config.addinivalue_line("markers", "smoke: a short real-model set (about 30 tests) that covers the pipeline end to end")
    if MODEL_ERROR:
        config.option.offline = True


def pytest_report_header(config: pytest.Config) -> list[str]:
    if MODEL_ERROR:
        return [f"model: none ({MODEL_ERROR}); running the offline tests, model tests are skipped. A key in .env runs everything."]
    return ["model: " + ("offline (--offline): model tests skipped" if config.getoption("--offline") else "the provider in .env")]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None, pytest.TestReport, None]:
    """With --offline and no key, a test stopped by the missing model is reported as skipped, never passed."""
    outcome = yield
    report = outcome.get_result()
    exc = call.excinfo
    if MODEL_ERROR and report.failed and exc is not None and isinstance(exc.value, RuntimeError) and str(exc.value) == MODEL_ERROR:
        report.outcome = "skipped"
        report.longrepr = (str(item.path), item.location[1] or 0, "needs a model (--offline)")


@pytest.fixture(scope="session", autouse=True)
def _registry() -> None:
    REGISTRY.discover()


@pytest.fixture(autouse=True)
def _no_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    """DeepEval judged metrics are an evals feature (`keelchat evals run`), not part of the unit suite."""
    monkeypatch.delenv("EVALS_JUDGE_MODEL", raising=False)


@pytest.fixture
def policy_yaml() -> str:
    return EXAMPLE.read_text()

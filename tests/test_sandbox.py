from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pandas as pd
import pytest
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.utils import AgentError

from tests.test_analytics import root, sales  # noqa: F401  (fixture)
from yachat.analytics.analyst import Analyst, Limits
from yachat.analytics.code_agent import SandboxExecutor, data_files, final_answer_code
from yachat.analytics.models import AnalyzeIn
from yachat.analytics.sandbox import MARK, FakeSandbox, LocalSandbox, RunOutput, parse, parse_agentcore, sandbox_from_env, wrap
from yachat.analytics.service import AnalyticsService
from yachat.reasoning import Check
from yachat.runtime.llm import ModelConfig
from yachat.runtime.llm_factory import llm_from_env


def _docker_sandbox() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", "yachat-sandbox:latest"], capture_output=True).returncode == 0


docker = pytest.mark.skipif(not _docker_sandbox(), reason="docker with the yachat-sandbox image (deploy/sandbox) not available")


def result_line(status: str, value: object = None, logs: str = "") -> RunOutput:
    return RunOutput(f"noise\n{MARK}{json.dumps({'status': status, 'value': value, 'logs': logs})}\n")


def test_run_protocol_and_backends_from_env(monkeypatch):
    assert parse(result_line("final", "safe:3")) == {"status": "final", "value": "safe:3", "logs": ""}
    assert parse(RunOutput("", "Killed", 137))["status"] == "error"
    script = wrap(["x = 2"], "print(x * 21)")
    assert "x = 2" in script and MARK in script
    stream = {
        "stream": [{"result": {"structuredContent": {"stdout": "a\n", "stderr": "", "exitCode": 0}}}, {"result": {"content": [{"type": "text", "text": "b"}]}}]
    }
    assert parse_agentcore(stream) == RunOutput("a\nb", "", 0)
    monkeypatch.delenv("YACHAT_SANDBOX", raising=False)
    assert sandbox_from_env() is None
    monkeypatch.setenv("YACHAT_SANDBOX", "agentcore")
    assert sandbox_from_env().name == "agentcore"
    monkeypatch.setenv("YACHAT_SANDBOX", "lambda")
    with pytest.raises(ValueError, match="expected local or agentcore"):
        sandbox_from_env()


def test_executor_replays_successful_steps_and_reads_final_answers():
    fake = FakeSandbox([result_line("ok", logs="42"), result_line("error", "NameError: y"), result_line("final", 'safe:{"slope": 1.5}')])
    ex = SandboxExecutor(fake, {"data.csv": "a\n1\n"}, "import pandas as pd", 5, AgentLogger(level=LogLevel.OFF))
    assert ex.run_code_raise_errors("x = 1\nprint(42)").logs == "42"
    with pytest.raises(AgentError, match="NameError"):
        ex.run_code_raise_errors("print(y)")
    out = ex.run_code_raise_errors("final_answer({'slope': 1.5})")
    assert out.is_final_answer and out.output == {"slope": 1.5}
    last_script, files = fake.calls[-1]
    assert files == {"data.csv": "a\n1\n"} and "x = 1" in last_script and "print(y)" not in last_script  # failed steps are not replayed
    assert ex.analysis_code == "x = 1\nprint(42)\n\nfinal_answer({'slope': 1.5})" and ex.runs == 3


def test_final_answer_rejects_an_answer_that_fails_its_checks():
    checks = [
        Check(id="c1", claim="p is a probability", code="assert 0 <= answer['p'] <= 1"),
        Check(id="c2", claim="n is the rows", code="assert answer['n'] == len(df)"),
    ]
    scope = {"df": pd.DataFrame({"a": [1, 2]}), "pd": pd}
    exec(final_answer_code(checks), scope)  # our prelude, as the sandbox runs it
    with pytest.raises(BaseException) as e:
        scope["final_answer"]({"p": 0.2, "n": 2})
    assert type(e.value).__name__ == "FinalAnswerException"
    with pytest.raises(AssertionError, match="c2: AssertionError") as e:
        scope["final_answer"]({"p": 0.2, "n": 3})
    assert "c1" not in str(e.value)
    exec(final_answer_code([]), scope)
    with pytest.raises(BaseException, match="safe:"):
        scope["final_answer"]({"anything": 1})  # no code checks: nothing to enforce


def test_data_files_keep_dates():
    files, prelude = data_files(pd.DataFrame({"month": pd.to_datetime(["2026-01-01"]), "v": [1]}))
    assert files["data.csv"].startswith("month,v") and 'parse_dates=["month"]' in prelude


@docker
def test_docker_sandbox_isolation():
    sb = LocalSandbox(backend="docker", memory_mb=256)
    run = lambda code, timeout=20: parse(sb.run(wrap([], code), {"data.csv": "a\n1\n2\n"}, timeout))  # noqa: E731
    assert run("import pandas as pd, scipy, statsmodels\nprint(pd.read_csv('data.csv').a.sum())") == {"status": "ok", "value": None, "logs": "3\n"}
    assert "URLError" in run("import urllib.request\nurllib.request.urlopen('http://example.com', timeout=3)")["value"]
    assert "FileNotFoundError" in run(f"open({str(Path.cwd() / '.env')!r}).read()")["value"]
    assert run("import os\nprint(os.getuid())")["logs"].strip() == "65534"  # nobody
    assert run("x = b'1' * (600 * 1024 * 1024)")["status"] == "error"  # memory limit (pages written; a zeroed bytearray is never committed)
    with pytest.raises(Exception, match="timed out"):
        sb.run(wrap([], "while True: pass"), {}, 2)
    left = subprocess.run(["docker", "ps", "-aq", "--filter", "ancestor=yachat-sandbox:latest"], capture_output=True, text=True).stdout
    assert left.strip() == ""  # every run's container is gone


@docker
async def test_analyst_runs_statistics_in_the_sandbox(root):  # noqa: F811
    analyst = Analyst(llm_from_env(), ModelConfig.from_env(), Limits(), LocalSandbox(backend="docker"))
    r = await AnalyticsService(analyst).analyze(
        AnalyzeIn(question="Is EU monthly revenue declining significantly over time? Fit a linear trend and give its p-value.", dataset=sales(root))
    )
    assert r.status in ("ok", "partial") and r.outcome.method == "sql+python", r.outcome
    assert r.data.sandbox == "local:docker" and r.data.code and r.data.code_output and r.outcome.attempts["code"] >= 1
    out = {k.lower(): v for k, v in r.data.code_output.items()}
    assert any("p" in k and isinstance(v, float) and v < 0.05 for k, v in out.items()), out  # EU falls every month
    v = r.verification
    assert v is not None and v.plan.method and v.checks and r.outcome.attempts["verify"] == v.rounds
    assert any(c.code for c in v.checks) and "_CHECKS" not in r.data.code  # tests written first, enforced by final_answer
    if not v.passed:  # never shown as verified
        assert r.status == "partial" and any("did not pass verification" in x for x in r.outcome.limitations)

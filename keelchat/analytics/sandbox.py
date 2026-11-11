"""Sandbox port: run model-written Python in an ephemeral, isolated environment. Never in the service process.

Adapters (KEELCHAT_SANDBOX):
- `local`     llm-sandbox. KEELCHAT_SANDBOX_BACKEND=bubblewrap (Linux, the production image) | docker | podman (laptops;
              image from deploy/sandbox). No network, CPU/memory/file/process limits, a fresh workspace per run.
- `agentcore` AWS Bedrock AgentCore Code Interpreter: one session per run, stopped afterwards.
- unset       no sandbox: the Analyst answers with SQL only.

Every run starts clean: the code, its input files and any state exist only for that run.
"""

from __future__ import annotations

import json
import os
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from keelchat.logging import get_logger

log = get_logger("sandbox")

#: the libraries model-written code may import (installed in deploy/sandbox and the production image)
LIBRARIES = ["pandas", "numpy", "scipy", "statsmodels", "sklearn", "math", "statistics", "json", "datetime", "collections", "itertools"]


class SandboxError(Exception):
    """The sandbox could not run the code (not: the code raised). Message is for logs only."""


@dataclass
class RunOutput:
    stdout: str
    stderr: str = ""
    exit_code: int = 0


class Sandbox(ABC):
    name: str = "sandbox"

    @abstractmethod
    def run(self, code: str, files: dict[str, str], timeout_s: float) -> RunOutput:
        """Run `code` (Python) in a fresh environment with `files` (name → text) in its working directory. Blocking."""


@dataclass
class FakeSandbox(Sandbox):
    """Returns scripted outputs; records what it was asked to run. For tests of the plumbing around a sandbox."""

    outputs: list[RunOutput] = field(default_factory=list)
    calls: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    name: str = "fake"

    def run(self, code: str, files: dict[str, str], timeout_s: float) -> RunOutput:
        self.calls.append((code, dict(files)))
        if not self.outputs:
            raise SandboxError("no scripted output left")
        return self.outputs.pop(0)


class LocalSandbox(Sandbox):
    """llm-sandbox: one session per run, closed (and its workspace deleted) afterwards."""

    name = "local"

    def __init__(self, backend: str | None = None, image: str | None = None, memory_mb: int | None = None, cpu_s: int | None = None):
        self.backend = backend or os.getenv("KEELCHAT_SANDBOX_BACKEND") or "bubblewrap"
        self.image = image or os.getenv("KEELCHAT_SANDBOX_IMAGE") or "keelchat-sandbox:latest"
        self.memory_mb = memory_mb or int(os.getenv("KEELCHAT_SANDBOX_MEMORY_MB") or 1024)
        self.cpu_s = cpu_s or int(os.getenv("KEELCHAT_SANDBOX_CPU_S") or 60)
        self.name = f"local:{self.backend}"

    def _session(self) -> Any:
        from llm_sandbox import SandboxSession

        if self.backend in ("docker", "podman"):
            return SandboxSession(
                backend=self.backend,
                lang="python",
                image=self.image,
                skip_environment_setup=True,  # nothing is installed at run time: the image has the libraries, no network
                runtime_configs={
                    "network_mode": "none",
                    "mem_limit": f"{self.memory_mb}m",
                    "pids_limit": 128,
                    "cap_drop": ["ALL"],
                    "cap_add": ["CHOWN"],  # llm-sandbox hands copied files to `user` as root; the code itself runs as nobody
                    "security_opt": ["no-new-privileges"],
                    "user": "nobody",
                    "init": True,  # PID 1 forwards SIGTERM, so closing the session does not wait out Docker's stop grace
                },
            )
        import sys

        from llm_sandbox.local.config import DEFAULT_READ_ONLY_PATHS, LocalSandboxConfig

        config = LocalSandboxConfig(
            network="none",
            python_executable=sys.executable,  # the service's interpreter, which has the analysis libraries
            # system paths plus the service's virtualenv, read-only; nothing else of the host (no app code, no secrets)
            read_only_paths=tuple(dict.fromkeys((*DEFAULT_READ_ONLY_PATHS, sys.prefix, sys.base_prefix))),
            memory_limit_bytes=self.memory_mb * 1024 * 1024,
            cpu_time_limit=self.cpu_s,
            file_size_limit=256 * 1024 * 1024,
        )
        return SandboxSession(backend=self.backend, lang="python", sandbox_config=config, skip_environment_setup=True)

    def run(self, code: str, files: dict[str, str], timeout_s: float) -> RunOutput:
        try:
            with tempfile.TemporaryDirectory(prefix="sandbox-in-") as tmp, self._session() as session:
                for name, text in files.items():
                    src = Path(tmp) / name
                    src.write_text(text)
                    session.copy_to_runtime(str(src), f"/sandbox/{name}")
                out = session.run(code, timeout=timeout_s)
        except Exception as e:  # backend unavailable, timeout, container failure: the code did not run to completion
            raise SandboxError(f"{e.__class__.__name__}: {e}") from e
        return RunOutput(out.stdout or "", out.stderr or "", int(out.exit_code or 0))


class AgentCoreSandbox(Sandbox):
    """AWS Bedrock AgentCore Code Interpreter. Use an interpreter created with network mode SANDBOX (no internet);
    KEELCHAT_AGENTCORE_INTERPRETER names it (default: the AWS-managed aws.codeinterpreter.v1)."""

    name = "agentcore"

    def __init__(self, region: str | None = None, identifier: str | None = None):
        self.region = region or os.getenv("AWS_REGION") or "us-east-1"
        self.identifier = identifier or os.getenv("KEELCHAT_AGENTCORE_INTERPRETER") or "aws.codeinterpreter.v1"

    def run(self, code: str, files: dict[str, str], timeout_s: float) -> RunOutput:
        from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter

        client = CodeInterpreter(self.region)
        try:
            client.start(identifier=self.identifier, session_timeout_seconds=max(60, int(timeout_s) + 30))
            for name, text in files.items():
                client.upload_file(path=name, content=text)
            return parse_agentcore(client.execute_code(code, clear_context=True))
        except SandboxError:
            raise
        except Exception as e:
            raise SandboxError(f"{e.__class__.__name__}: {e}") from e
        finally:
            try:
                client.stop()  # the session and everything in it is discarded
            except Exception as e:
                log.error("agentcore session stop failed", error=e.__class__.__name__)


def parse_agentcore(response: dict[str, Any]) -> RunOutput:
    """AgentCore streams result events; structuredContent carries stdout, stderr and exitCode."""
    stdout, stderr, code = [], [], 0
    for event in response.get("stream", []):
        result = event.get("result") or {}
        sc = result.get("structuredContent") or {}
        if sc:
            stdout.append(str(sc.get("stdout", "")))
            stderr.append(str(sc.get("stderr", "")))
            code = int(sc.get("exitCode", 0) or 0)
        else:
            stdout += [str(c.get("text", "")) for c in result.get("content", []) if c.get("type") == "text"]
        if result.get("isError") and not code:
            code = 1
    return RunOutput("".join(stdout), "".join(stderr), code)


def sandbox_from_env() -> Sandbox | None:
    kind = (os.getenv("KEELCHAT_SANDBOX") or "").strip()
    if not kind:
        return None
    if kind == "local":
        return LocalSandbox()
    if kind == "agentcore":
        return AgentCoreSandbox()
    raise ValueError(f"KEELCHAT_SANDBOX={kind!r}: expected local or agentcore")


# ---------------------------------------------------------------- the run protocol (same for every backend)

MARK = "__CHATTER_RESULT__"

_RUNNER = """
import contextlib, io, json, sys, traceback
_g = {"__name__": "__main__"}
_steps = json.loads(%(steps)r)
_code = %(code)r
with contextlib.redirect_stdout(io.StringIO()):
    for _s in _steps:
        exec(compile(_s, "<previous>", "exec"), _g)
_buf, _out = io.StringIO(), {"status": "ok", "value": None}
try:
    with contextlib.redirect_stdout(_buf):
        exec(compile(_code, "<step>", "exec"), _g)
except BaseException as _e:
    if type(_e).__name__ == "FinalAnswerException":
        _out = {"status": "final", "value": _e.value}
    else:
        _out = {"status": "error", "value": "".join(traceback.format_exception_only(type(_e), _e))[-3000:]}
_out["logs"] = _buf.getvalue()[-20000:]
print(%(mark)r + json.dumps(_out, default=str))
"""


def wrap(steps: list[str], code: str) -> str:
    """A script that replays earlier successful steps silently (sandboxes are stateless), runs `code`, and prints one
    marked JSON line: status ok | final | error, the final value, and the step's printed output."""
    return _RUNNER % {"steps": json.dumps(steps), "code": code, "mark": MARK}


def parse(out: RunOutput) -> dict[str, Any]:
    for line in reversed(out.stdout.splitlines()):
        if line.startswith(MARK):
            parsed: dict[str, Any] = json.loads(line[len(MARK) :])
            return parsed
    tail = (out.stderr or out.stdout)[-2000:]
    return {"status": "error", "value": f"the run ended without a result (exit {out.exit_code}): {tail}", "logs": ""}

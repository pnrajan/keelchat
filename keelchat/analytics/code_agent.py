"""The Analyst's statistics step: a smolagents CodeAgent writes pandas/statistics code, which runs in the Sandbox.

smolagents owns the loop (write code → run → read the error → rewrite) and the final_answer protocol. We supply:
- LLMClientModel: smolagents' model interface over our LLMClient (model ids from config; calls counted);
- SandboxExecutor: smolagents' remote-executor interface over our Sandbox port. Sandboxes are stateless, so each step
  replays the earlier successful steps before running the new one.
Steps are capped at 1 + repairs; when the cap is hit without a final answer, the step has failed (no guessed answer).
The checks written before the code (keelchat/reasoning) run inside final_answer: an answer that fails one is
rejected like an error, and the agent fixes its analysis."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pandas as pd
from smolagents import CodeAgent
from smolagents.models import ChatMessage, MessageRole, Model
from smolagents.monitoring import AgentLogger, LogLevel
from smolagents.remote_executors import RemotePythonExecutor
from smolagents.tools import Tool
from smolagents.utils import AgentError

from keelchat.analytics.sandbox import LIBRARIES, Sandbox, parse, wrap
from keelchat.logging import get_logger
from keelchat.reasoning import Check
from keelchat.runtime.llm import LLMClient, LLMRequest

log = get_logger("code_agent")

FINAL_ANSWER = """
import json as _json


class FinalAnswerException(BaseException):
    def __init__(self, value):
        self.value = value


_CHECKS = _json.loads({checks!r})


def final_answer(answer):
    _failed = []
    for _id, _src in _CHECKS:
        try:
            exec(_src, {{"answer": answer, "df": df, "pd": pd}})
        except Exception as _e:
            _failed.append(f"{{_id}}: {{_e.__class__.__name__}}: {{_e}}")
    if _failed:
        raise AssertionError("final_answer rejected the answer; these checks failed: " + "; ".join(_failed) + ". Fix the analysis.")
    raise FinalAnswerException("safe:" + _json.dumps(answer, default=lambda o: o.item() if hasattr(o, "item") else str(o)))
"""


def final_answer_code(checks: Sequence[Check]) -> str:
    """final_answer for the sandbox, with the code checks it must pass (as data: JSON in a string literal)."""
    return FINAL_ANSWER.format(checks=json.dumps([[c.id, c.code] for c in checks]))


_ROLE = {"tool-call": "assistant", "tool-response": "user", "assistant": "assistant"}


class LLMClientModel(Model):  # type: ignore[misc]
    """smolagents → LLMClient. Runs in the agent's worker thread; calls go to the service's event loop."""

    def __init__(self, llm: LLMClient, model: str, loop: asyncio.AbstractEventLoop, timeout_s: float = 120.0):
        super().__init__(model_id=model, flatten_messages_as_text=True)
        self.llm, self.loop, self.timeout_s = llm, loop, timeout_s
        self.calls = 0

    def generate(self, messages: list[Any], stop_sequences: list[str] | None = None, **kwargs: Any) -> ChatMessage:
        prepared = self._prepare_completion_kwargs(messages=messages, stop_sequences=stop_sequences)["messages"]
        system = "\n\n".join(str(m["content"]) for m in prepared if m["role"] == "system")
        msgs = [{"role": _ROLE.get(str(m["role"]), "user"), "content": str(m["content"])} for m in prepared if m["role"] != "system"]
        req = LLMRequest("analytics_code", self.model_id, system, msgs, max_tokens=2000, effort="medium", stop=list(stop_sequences or []))
        self.calls += 1
        text = asyncio.run_coroutine_threadsafe(self.llm.complete(req), self.loop).result(timeout=self.timeout_s)
        return ChatMessage(role=MessageRole.ASSISTANT, content=text)


class SandboxExecutor(RemotePythonExecutor):  # type: ignore[misc]
    def __init__(self, sandbox: Sandbox, files: dict[str, str], prelude: str, timeout_s: float, logger: AgentLogger, checks: Sequence[Check] = ()):
        super().__init__(additional_imports=[], logger=logger)
        self.sandbox, self.files, self.timeout_s = sandbox, files, timeout_s
        self.final = final_answer_code(checks)
        self.steps = [prelude]
        self.runs = 0
        self._ours = 1  # prelude and tool definitions: replayed, but not part of the analysis code

    @property
    def analysis_code(self) -> str:
        return "\n\n".join(self.steps[self._ours :])

    def send_tools(self, tools: dict[str, Tool]) -> None:
        """Only final_answer is offered, defined in the replayed prelude in smolagents' "safe:" JSON format, so the
        sandbox needs no smolagents (its generated tool code imports packages the sandbox does not have)."""
        self.steps.append(self.final)
        self._ours = len(self.steps)

    def send_variables(self, variables: dict[str, Any]) -> None:
        pass  # the data arrives as files; agent state is not used

    def install_packages(self, additional_imports: list[str]) -> list[str]:
        return additional_imports  # nothing is installed at run time; the sandbox image has the libraries

    def run_code_raise_errors(self, code: str) -> Any:
        from smolagents.local_python_executor import CodeOutput

        self.runs += 1
        out = parse(self.sandbox.run(wrap(self.steps, code), self.files, self.timeout_s))
        logs = str(out.get("logs") or "")
        if out["status"] == "error":
            raise AgentError(f"{logs}\nExecuting code yielded an error:\n{out['value']}", self.logger)
        self.steps.append(code)
        if out["status"] == "final":
            return CodeOutput(output=self._deserialize_final_answer(out["value"], self.allow_pickle), logs=logs, is_final_answer=True)
        return CodeOutput(output=None, logs=logs, is_final_answer=False)


@dataclass
class CodeRun:
    output: dict[str, Any] | None
    code: str
    steps: int
    llm_calls: int
    error: str | None = None


TASK = """Answer this analysis question with Python: {question}

`df` is a pandas DataFrame already loaded with the SQL result below ({rows} rows). Columns and types:
{columns}
First rows:
{head}

Use pandas, numpy, scipy or statsmodels (allowed imports: {libraries}). There is no network and no other data.
Compute what the question needs (for example a trend slope with its p-value, a correlation, a test, a regression).
Finish by calling final_answer with a dict of plain numbers and short strings, for example
{{"method": "OLS of revenue on month index", "slope": 412.5, "p_value": 0.03, "n": 6}}. Do not print the whole table.
{approach}"""
CHECKS_NOTE = """
final_answer runs these checks on your answer and rejects it if one fails; make the answer pass them (use these keys):
{checks}"""


def data_files(frame: pd.DataFrame) -> tuple[dict[str, str], str]:
    """The SQL result as CSV plus the prelude that loads it with its types."""
    dates = [str(c) for c in frame.columns if pd.api.types.is_datetime64_any_dtype(frame[c])]
    prelude = "import pandas as pd\n" + f"df = pd.read_csv('data.csv', parse_dates={json.dumps(dates)})\n"
    return {"data.csv": frame.to_csv(index=False)}, prelude


def run_code_agent(
    llm: LLMClient,
    model: str,
    loop: asyncio.AbstractEventLoop,
    sandbox: Sandbox,
    question: str,
    frame: pd.DataFrame,
    repairs: int,
    timeout_s: float = 60.0,
    approach: str = "",
    checks: Sequence[Check] = (),
    feedback: str = "",
) -> CodeRun:
    """Blocking: run in a worker thread. `approach`: the plan; `checks`: the runnable checks final_answer enforces;
    `feedback`: problems a verifier found with the previous attempt."""
    files, prelude = data_files(frame)
    logger = AgentLogger(level=LogLevel.OFF)  # smolagents logs include data; ours carry counts only
    executor = SandboxExecutor(sandbox, files, prelude, timeout_s, logger, checks)
    llm_model = LLMClientModel(llm, model, loop)
    agent = CodeAgent(tools=[], model=llm_model, executor=executor, additional_authorized_imports=LIBRARIES, max_steps=repairs + 1, logger=logger)
    columns = "\n".join(f"- {c}: {t}" for c, t in frame.dtypes.astype(str).items())
    note = CHECKS_NOTE.format(checks="\n".join(f"- {c.id}: {c.code}" for c in checks)) if checks else ""
    guidance = "\n\n".join(x for x in (approach, note, feedback) if x)
    task = TASK.format(
        question=question, rows=len(frame), columns=columns, head=frame.head(5).to_csv(index=False), libraries=", ".join(LIBRARIES), approach=guidance
    )
    try:
        result = agent.run(task, return_full_result=True)
    except Exception as e:  # smolagents surfaces model and executor failures as exceptions
        return CodeRun(None, executor.analysis_code, executor.runs, llm_model.calls, f"{e.__class__.__name__}: {e}")
    code = executor.analysis_code
    if result.state != "success" or not isinstance(result.output, dict):
        return CodeRun(None, code, executor.runs, llm_model.calls, f"no final answer ({result.state})")
    return CodeRun(dict(result.output), code, executor.runs, llm_model.calls)

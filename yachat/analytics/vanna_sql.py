"""Question → SQL with Vanna's one-shot generator (legacy VannaBase: schema, docs and example pairs in the prompt).

Vanna is synchronous; it runs in a worker thread and its model calls are sent back to the service's event loop through
our LLMClient, so model ids come from config and every call is counted. Vanna's own logging (which prints prompts,
including data) is silenced."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pandas as pd
from vanna.legacy.base.base import VannaBase

from yachat.runtime.llm import LLMClient, LLMRequest

Message = dict[str, str]


class _Vanna(VannaBase):
    def __init__(
        self,
        ddl: list[str],
        docs: list[str],
        examples: list[dict[str, str]],
        submit: Callable[[list[Message]], str],
        run: Callable[[str], pd.DataFrame],
    ):
        super().__init__(config={"dialect": "DuckDB"})  # type: ignore[no-untyped-call]
        self._ddl, self._docs, self._examples, self._submit, self._run = ddl, docs, examples, submit, run
        self.run_sql_is_set = True

    def run_sql(self, sql: str, **kwargs: Any) -> pd.DataFrame:
        return self._run(sql)

    # the whole (small) schema is the context: no vector retrieval
    def get_related_ddl(self, question: str, **kwargs: Any) -> list[str]:
        return list(self._ddl)

    def get_related_documentation(self, question: str, **kwargs: Any) -> list[str]:
        return list(self._docs)

    def get_similar_question_sql(self, question: str, **kwargs: Any) -> list[dict[str, str]]:
        return list(self._examples)

    def system_message(self, message: str) -> Message:
        return {"role": "system", "content": message}

    def user_message(self, message: str) -> Message:
        return {"role": "user", "content": message}

    def assistant_message(self, message: str) -> Message:
        return {"role": "assistant", "content": message}

    def submit_prompt(self, prompt: list[Message], **kwargs: Any) -> str:
        return self._submit(prompt)

    def log(self, message: str, title: str = "Info") -> None:
        pass

    # training storage is unused: context comes from the dataset on every call
    def generate_embedding(self, data: str, **kwargs: Any) -> list[float]:
        return []

    def add_question_sql(self, question: str, sql: str, **kwargs: Any) -> str:
        raise NotImplementedError

    def add_ddl(self, ddl: str, **kwargs: Any) -> str:
        raise NotImplementedError

    def add_documentation(self, documentation: str, **kwargs: Any) -> str:
        raise NotImplementedError

    def get_training_data(self, **kwargs: Any) -> pd.DataFrame:
        return pd.DataFrame()

    def remove_training_data(self, id: str, **kwargs: Any) -> bool:
        return False


class VannaSQL:
    """Async facade over Vanna for one dataset. `calls` counts model calls made through it."""

    def __init__(
        self,
        llm: LLMClient,
        model: str,
        ddl: list[str],
        docs: list[str],
        examples: list[dict[str, str]],
        query: Callable[[str], Coroutine[Any, Any, pd.DataFrame]],
        *,
        timeout_s: float = 120.0,
    ):
        self.llm, self.model, self.timeout_s = llm, model, timeout_s
        self.calls = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._query = query
        self._vn = _Vanna(ddl, docs, examples, self._submit, self._run_sql)

    def _run_sql(self, sql: str) -> pd.DataFrame:
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(self._query(sql), self._loop).result(timeout=self.timeout_s)

    def _submit(self, prompt: list[Message]) -> str:
        assert self._loop is not None
        system = "\n\n".join(m["content"] for m in prompt if m["role"] == "system")
        messages = [m for m in prompt if m["role"] != "system"]
        req = LLMRequest("analytics_sql", self.model, system, messages, max_tokens=1500, effort="medium")
        self.calls += 1
        return asyncio.run_coroutine_threadsafe(self.llm.complete(req), self._loop).result(timeout=self.timeout_s)

    async def _in_thread(self, fn: Callable[..., str], *args: Any, **kwargs: Any) -> str:
        self._loop = asyncio.get_running_loop()
        return await asyncio.to_thread(fn, *args, **kwargs)

    async def generate_sql(self, question: str) -> str:
        # allow_llm_to_see_data: Vanna may first list a column's distinct values (through the locked runner)
        return await self._in_thread(self._vn.generate_sql, question, allow_llm_to_see_data=True)

"""The judge model for DeepEval and Ragas metrics, over litellm (SiliconFlow, OpenRouter, OpenAI), so evals need no
Anthropic credit. `EVALS_JUDGE_MODEL` names it; unset, only the expectations checked in code run."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from pydantic import BaseModel

from keelchat.runtime.litellm_client import LiteLLMClient
from keelchat.runtime.llm import LLMRequest

SYSTEM = "You are a strict evaluation judge."
MAX_TOKENS = 2000


def judge_model_from_env() -> str | None:
    """The judge's model id, e.g. openrouter/openai/gpt-4o-mini or siliconflow/Qwen/Qwen3.5-27B."""
    return os.getenv("EVALS_JUDGE_MODEL") or None


async def _structured(client: LiteLLMClient, model: str, prompt: str, schema: Any) -> Any:
    req = LLMRequest("judge", model, SYSTEM, [{"role": "user", "content": prompt}], max_tokens=MAX_TOKENS)
    return schema.model_validate(await client.structured(req, schema.model_json_schema()))


async def structured_verdict[T: BaseModel](model: str, prompt: str, schema: type[T]) -> T:
    """One judge call answered as `schema`."""
    out: T = await _structured(LiteLLMClient(), model, prompt, schema)
    return out


def deepeval_judge(model: str) -> Any:
    """A DeepEvalBaseLLM backed by `model`."""
    from deepeval.models.base_model import DeepEvalBaseLLM

    client = LiteLLMClient()  # provider key/base resolved per request from the model id

    class LiteLLMJudge(DeepEvalBaseLLM):  # type: ignore[no-untyped-call]
        def load_model(self) -> Any:
            return client

        def generate(self, prompt: str, schema: Any = None) -> Any:
            return asyncio.run(self.a_generate(prompt, schema))

        async def a_generate(self, prompt: str, schema: Any = None) -> Any:
            if schema is not None:
                return await _structured(client, model, prompt, schema)
            return await client.complete(LLMRequest("judge", model, SYSTEM, [{"role": "user", "content": prompt}], max_tokens=MAX_TOKENS))

        def get_model_name(self) -> str:
            return model

    return LiteLLMJudge()


def ragas_judge(model: str) -> Any:
    """A Ragas structured-output LLM backed by `model`."""
    from ragas.llms.base import InstructorBaseRagasLLM

    client = LiteLLMClient()

    class LiteLLMRagasJudge(InstructorBaseRagasLLM):
        def generate(self, prompt: str, response_model: Any) -> Any:
            return asyncio.run(self.agenerate(prompt, response_model))

        async def agenerate(self, prompt: str, response_model: Any) -> Any:
            return await _structured(client, model, prompt, response_model)

    return LiteLLMRagasJudge()

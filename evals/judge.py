"""An LLM judge for DeepEval metrics over litellm (SiliconFlow / OpenRouter / OpenAI), so evals need no Anthropic credit."""

from __future__ import annotations

import os
from typing import Any

from yachat.runtime.litellm_client import LiteLLMClient
from yachat.runtime.llm import LLMRequest


def judge_model_from_env() -> Any | None:
    """A DeepEvalBaseLLM when EVALS_JUDGE_MODEL is set (e.g. openrouter/openai/gpt-4o-mini or siliconflow/Qwen/Qwen3.5-27B)."""
    model = os.getenv("EVALS_JUDGE_MODEL")
    if not model:
        return None
    from deepeval.models.base_model import DeepEvalBaseLLM

    client = LiteLLMClient()  # provider key/base resolved per request from the model id

    class LiteLLMJudge(DeepEvalBaseLLM):  # type: ignore[misc]
        def load_model(self) -> Any:
            return client

        def generate(self, prompt: str, schema: Any = None) -> Any:
            import asyncio

            return asyncio.run(self.a_generate(prompt, schema))

        async def a_generate(self, prompt: str, schema: Any = None) -> Any:
            req = LLMRequest("judge", model, "You are a strict evaluation judge.", [{"role": "user", "content": prompt}], max_tokens=1500)
            if schema is not None:
                out = await client.structured(req, schema.model_json_schema())
                return schema.model_validate(out)
            return await client.complete(req)

        def get_model_name(self) -> str:
            return model

    return LiteLLMJudge()

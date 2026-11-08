"""LLMClient over litellm: any provider litellm supports (OpenAI, SiliconFlow, OpenRouter, Bedrock ...).

The Anthropic SDK client (runtime/llm.py) stays the default for Claude; this one is for other providers or routing.
Structured output = tool call with the JSON schema, same contract as the Anthropic client.
"""

from __future__ import annotations

import json
import os
from typing import Any

from yachat.runtime.llm import LLMClient, LLMError, LLMRequest
from yachat.runtime.usage import metered


def _provider_model(req: LLMRequest) -> str:
    """The model litellm is asked for (priced when no gateway names the one it routed to)."""
    from yachat.runtime.llm_factory import litellm_settings

    return str(litellm_settings(req.model)["model_name"])


class LiteLLMClient(LLMClient):
    def __init__(self, api_key: str | None = None, api_base: str | None = None, timeout_s: float = 30.0, **defaults: Any):
        self.api_key, self.api_base, self.timeout_s, self.defaults = api_key, api_base, timeout_s, defaults

    async def _call(self, req: LLMRequest, **extra: Any) -> Any:
        from yachat.tracing import span

        with span("yachat.model", req.purpose, model=req.model, purpose=req.purpose):
            return await self._call_untraced(req, **extra)

    async def _call_untraced(self, req: LLMRequest, **extra: Any) -> Any:
        from yachat.runtime.llm_factory import litellm_settings  # per-request: generate and classify may be different providers

        settings = litellm_settings(req.model)
        model = str(settings["model_name"])
        extra_body: dict[str, Any] = {}
        if req.model.startswith("siliconflow/") and not os.getenv("LITELLM_PROXY_URL"):  # via a gateway, its config owns this
            extra_body["enable_thinking"] = False  # Qwen3 hybrid models: reasoning would consume max_tokens and empty the reply
        kwargs: dict[str, Any] = {
            "extra_body": extra_body,
            "model": model,
            "messages": [{"role": "system", "content": req.system}, *req.messages],
            "max_tokens": req.max_tokens,
            "timeout": req.timeout_s or self.timeout_s,
            "num_retries": 4,  # litellm backs off on 429/5xx; provider TPM limits are hit by parallel tests and bursts
            **({"stop": req.stop} if req.stop else {}),
            **self.defaults,
            **extra,
        }
        if self.api_key or settings["api_key"]:
            kwargs["api_key"] = self.api_key or settings["api_key"]
        if self.api_base or settings["api_base"]:
            kwargs["api_base"] = self.api_base or settings["api_base"]
        return await self._send(req, **kwargs)

    @metered(provider_model=_provider_model)
    async def _send(self, req: LLMRequest, **kwargs: Any) -> Any:
        import litellm

        try:
            return await litellm.acompletion(**kwargs)
        except Exception as e:  # litellm raises provider-specific subclasses of its own exceptions
            raise LLMError(f"{e.__class__.__name__}: {str(e)[:160]}") from e

    async def complete(self, req: LLMRequest) -> str:
        resp = await self._call(req)
        return str(resp.choices[0].message.content or "")  # reasoning_content is never a reply

    async def structured(self, req: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        name = f"emit_{req.purpose}"
        # newer Claude models refuse a forced tool choice (as the Anthropic client does, they are left to pick it)
        choice: Any = "auto" if "claude" in req.model else {"type": "function", "function": {"name": name}}
        last: LLMError | None = None
        for attempt in range(2):  # open-weight models occasionally emit a truncated/malformed tool call; one retry
            resp = await self._call(
                req,
                tools=[{"type": "function", "function": {"name": name, "parameters": schema}}],
                tool_choice=choice,
            )
            calls = resp.choices[0].message.tool_calls or []
            if not calls:
                last = LLMError(f"model did not call {name}")
                continue
            try:
                return dict(json.loads(calls[0].function.arguments))
            except json.JSONDecodeError as e:
                last = LLMError(f"tool arguments were not valid JSON (attempt {attempt + 1})")
                last.__cause__ = e
        # last resort: JSON mode in the message body (some open-weight models truncate tool-call arguments)
        json_req = LLMRequest(
            req.purpose,
            req.model,
            req.system + "\nReply with a single JSON object matching this schema and nothing else:\n" + json.dumps(schema),
            req.messages,
            max_tokens=req.max_tokens,
            effort=req.effort,
            metadata=req.metadata,
        )
        resp = await self._call(json_req, response_format={"type": "json_object"})
        text = str(resp.choices[0].message.content or "").strip()
        try:
            return dict(json.loads(text[text.find("{") : text.rfind("}") + 1]))
        except (json.JSONDecodeError, ValueError) as e:
            assert last is not None
            raise last from e

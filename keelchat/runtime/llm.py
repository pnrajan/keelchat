"""LLMClient interface, model config, and the Claude API implementation.

Every classification call uses structured output (a tool with a JSON schema); every call names its model.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, replace
from dataclasses import field as dc_field
from typing import Any

from keelchat.logging import get_logger
from keelchat.runtime.usage import metered

log = get_logger("llm")


@dataclass(frozen=True)
class ModelConfig:
    generate: str = "claude-opus-5-5"
    classify: str = "claude-haiku-5-5"
    effort_generate: str = "medium"
    effort_classify: str = "low"
    #: rephrasing a finished reply and checking it: low by default, since the content is already decided
    effort_rewrite: str = "low"
    #: checks reasoning before it is shown (keelchat/reasoning); empty: the generate model
    verify: str = ""
    #: a stronger model that judges trial conversations and suggests fixes (keelchat/evals/critic); empty: off
    critic: str = ""

    @property
    def verifier(self) -> str:
        return self.verify or self.generate

    @classmethod
    def from_env(cls) -> ModelConfig:
        return cls(
            generate=os.getenv("KEELCHAT_MODEL_GENERATE", cls.generate),
            classify=os.getenv("KEELCHAT_MODEL_CLASSIFY", cls.classify),
            verify=os.getenv("KEELCHAT_MODEL_VERIFY", ""),
            critic=os.getenv("KEELCHAT_MODEL_CRITIC", ""),
            effort_generate=os.getenv("KEELCHAT_EFFORT_GENERATE") or cls.effort_generate,
            effort_classify=os.getenv("KEELCHAT_EFFORT_CLASSIFY") or cls.effort_classify,
            effort_rewrite=os.getenv("KEELCHAT_EFFORT_REWRITE") or cls.effort_rewrite,
        )


def small_only(m: ModelConfig) -> ModelConfig:
    """Every job on the small (classify) model: replies, checks and drafting included."""
    return replace(m, generate=m.classify, verify=m.classify, critic=m.classify if m.critic else "")


@dataclass
class LLMRequest:
    purpose: str
    model: str
    system: str
    messages: list[dict[str, str]]
    max_tokens: int = 1024
    effort: str = "low"
    #: stop generating at any of these (code agents stop before a made-up observation)
    stop: list[str] = dc_field(default_factory=list)
    #: seconds before the call gives up; the client's default when unset (long judgements need more)
    timeout_s: float | None = None
    #: fakes read this; real clients ignore it
    metadata: dict[str, Any] = dc_field(default_factory=dict)

    @property
    def last_user_text(self) -> str:
        for m in reversed(self.messages):
            if m["role"] == "user":
                return m["content"]
        return ""


def untrusted(tag: str, body: str, **attrs: str) -> str:
    """Fence data (tool output, query rows, an answer under review) for a prompt. `<` is escaped so the data cannot
    close the fence; the model is told the block is data, never instructions."""
    extra = "".join(f' {k}="{v}"' for k, v in attrs.items())
    return f'<{tag}{extra} trusted="false">\n{body.replace("<", "\\u003c")}\n</{tag}>'


class LLMError(Exception):
    pass


class LLMClient(ABC):
    @abstractmethod
    async def complete(self, req: LLMRequest) -> str: ...

    @abstractmethod
    async def structured(self, req: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]: ...


class AnthropicLLMClient(LLMClient):
    """Claude API via the official SDK. Classification = forced tool use with the schema as input_schema."""

    def __init__(self, api_key: str | None = None, timeout_s: float = 30.0):
        from anthropic import AsyncAnthropic

        key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not key and not os.getenv("ANTHROPIC_AUTH_TOKEN"):
            raise LLMError("ANTHROPIC_API_KEY (or ANTHROPIC_AUTH_TOKEN / ant auth profile) is required for AnthropicLLMClient")
        self._client = AsyncAnthropic(api_key=key, timeout=timeout_s)

    async def _create(self, req: LLMRequest, **extra: Any) -> Any:
        from keelchat.tracing import span

        with span("keelchat.model", req.purpose, model=req.model, purpose=req.purpose):
            return await self._create_untraced(req, **extra)

    async def _create_untraced(self, req: LLMRequest, **extra: Any) -> Any:
        resp = await self._send(req, **extra)
        if resp.stop_reason == "refusal":  # billed: the call was made
            raise LLMError("refused by safety classifier")
        return resp

    @metered()
    async def _send(self, req: LLMRequest, **extra: Any) -> Any:
        from anthropic import APIConnectionError, APIStatusError, RateLimitError

        try:
            return await self._client.beta.messages.create(  # type: ignore[call-overload]
                model=req.model,
                max_tokens=req.max_tokens,
                system=req.system,
                messages=req.messages,
                output_config={"effort": req.effort},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                **({"stop_sequences": req.stop} if req.stop else {}),
                **({"timeout": req.timeout_s} if req.timeout_s else {}),
                **extra,
            )
        except RateLimitError as e:
            raise LLMError("rate limited") from e
        except APIStatusError as e:
            msg = getattr(e, "message", "") or str(e)
            raise LLMError(f"api status {e.status_code}: {msg[:160]}") from e
        except APIConnectionError as e:
            raise LLMError("connection error") from e

    async def complete(self, req: LLMRequest) -> str:
        resp = await self._create(req)
        return str(next((b.text for b in resp.content if b.type == "text"), ""))

    async def structured(self, req: LLMRequest, schema: dict[str, Any]) -> dict[str, Any]:
        tool_name = f"emit_{req.purpose}"
        resp = await self._create(
            req,
            tools=[{"name": tool_name, "description": f"Record the {req.purpose} result.", "strict": True, "input_schema": schema}],
            tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        )
        for b in resp.content:
            if b.type == "tool_use" and b.name == tool_name:
                return dict(b.input)
        raise LLMError(f"model did not call {tool_name}")

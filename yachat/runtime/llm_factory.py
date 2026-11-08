"""Real LLM clients from configuration. There is no fake model: tests, sims and evals run against the configured provider."""

from __future__ import annotations

import os
from typing import Any

from yachat.runtime.llm import AnthropicLLMClient, LLMClient, ModelConfig

#: tools that reach an HTTP backend, keyed by the env var that configures it (used to decide what FakeTools may replace)
TOOL_BACKENDS: dict[str, str] = {
    "job_search": "JOB_SEARCH_URL",
    "submit_application": "JOB_SEARCH_URL",
    "knowledge_search": "KNOWLEDGE_URL",
    "skills_search": "CAREER_URL",
    "role_search": "CAREER_URL",
    "analytics_query": "ANALYTICS_URL",
    "handoff_webhook": "HANDOFF_URL",
}


def litellm_settings(model: str) -> dict[str, Any]:
    """Map a model id to litellm kwargs.

    Routing options, outermost first:
    - HEADROOM_PROXY_URL: a Headroom proxy (context compression, OpenAI-compatible) sits in front; it forwards to its
      own upstream, normally the LiteLLM gateway.
    - LITELLM_PROXY_URL: a LiteLLM gateway owns provider routing, fallbacks, retries and cost logging; the model id is
      the gateway's alias as configured there.
    - Neither: the id is `provider/model` called directly: SiliconFlow is OpenAI-compatible; OpenRouter is native.
    """
    gateway = os.getenv("LITELLM_PROXY_URL")
    headroom = os.getenv("HEADROOM_PROXY_URL")
    if headroom or gateway:
        base = (headroom or gateway or "").rstrip("/")
        if headroom and not base.endswith("/v1"):
            base += "/v1"  # headroom serves OpenAI-compatible chat at /v1
        key = os.getenv("LITELLM_PROXY_KEY") or os.getenv("OPENAI_API_KEY") or "unused"
        return {"model_name": "openai/" + model, "api_key": key, "api_base": base}
    if model.startswith("siliconflow/"):
        return {
            "model_name": "openai/" + model.split("/", 1)[1],
            "api_key": os.getenv("SILICONFLOW_API_KEY"),
            "api_base": (os.getenv("SILICONFLOW_BASE_URL") or "https://api.siliconflow.com/v1"),
        }
    if model.startswith("openrouter/"):
        return {"model_name": model, "api_key": os.getenv("OPENROUTER_API_KEY"), "api_base": None}
    return {"model_name": model, "api_key": None, "api_base": None}


def llm_from_env() -> LLMClient:
    """YACHAT_LLM=anthropic|litellm (default: anthropic when ANTHROPIC_API_KEY is set). A missing key raises."""
    choice = os.getenv("YACHAT_LLM") or ("anthropic" if os.getenv("ANTHROPIC_API_KEY") else "")
    if choice == "anthropic":
        if not os.getenv("ANTHROPIC_API_KEY"):
            raise RuntimeError("YACHAT_LLM=anthropic needs ANTHROPIC_API_KEY")
        return AnthropicLLMClient()
    if choice == "litellm":
        from yachat.runtime.litellm_client import LiteLLMClient

        settings = litellm_settings(ModelConfig.from_env().generate)
        if not settings["api_key"] and not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("YACHAT_LLM=litellm needs the provider key for YACHAT_MODEL_GENERATE (or LITELLM_PROXY_URL)")
        return LiteLLMClient()  # key and base per request (litellm_settings): each model may use another provider
    raise RuntimeError("no LLM configured: set YACHAT_LLM=anthropic|litellm and the provider key (see .env / README)")


def fake_tools_from_env() -> dict[str, Any]:
    """YACHAT_TOOLS=fake: FakeTools stand in for every HTTP tool whose backend env var is unset."""
    if os.getenv("YACHAT_TOOLS") != "fake":
        return {}
    from yachat.testing.fakes import FakeTools

    return {name: fn for name, fn in FakeTools().impls().items() if not os.getenv(TOOL_BACKENDS.get(name, ""))}

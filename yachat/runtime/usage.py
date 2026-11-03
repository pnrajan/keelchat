"""Model usage and cost. Each LLM client reports every call (model, purpose, tokens, time) to the meter of the turn it
runs in; the runtime stores them in the tenant's audit log. Prices come from YACHAT_MODEL_PRICES, else from
OpenRouter's public model list (refreshed daily), else litellm's price list; a model with no known price is recorded
with tokens and no cost."""

from __future__ import annotations

import json
import os
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from yachat.logging import get_logger

log = get_logger("usage")


@dataclass
class Usage:
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    #: input tokens read from the provider's prompt cache (billed lower), included in input_tokens' total cost below
    cached_tokens: int = 0
    ms: float = 0.0
    cost_usd: float | None = None


#: the reply when a tenant's spending limit is reached (no model is called)
LIMIT_REACHED = "This assistant has reached its usage limit for now. Please try again later."


def unpriced_rate() -> float:
    """USD per million tokens charged against a limit for a call with no known price, so an unpriced model is never free."""
    return float(os.getenv("YACHAT_UNPRICED_USD_PER_MTOK") or 15.0)


def charge_usd(calls: list[Usage]) -> float:
    """What the calls count against a spending limit: their price, or the unpriced rate when the price is unknown."""
    return sum(c.cost_usd if c.cost_usd is not None else (c.input_tokens + c.output_tokens) * unpriced_rate() / 1_000_000 for c in calls)


#: the calls of the turn (or background task) running now; None outside a turn (nothing is recorded)
METER: ContextVar[list[Usage] | None] = ContextVar("usage_meter", default=None)


@lru_cache(maxsize=1)
def price_overrides() -> dict[str, tuple[float, float]]:
    """YACHAT_MODEL_PRICES='{"claude-opus-5-5": [5, 25]}': USD per million input and output tokens."""
    raw = os.getenv("YACHAT_MODEL_PRICES") or ""
    if not raw.strip():
        return {}
    try:
        return {str(k): (float(v[0]), float(v[1])) for k, v in json.loads(raw).items()}
    except (ValueError, TypeError, IndexError, AttributeError) as e:
        log.error("YACHAT_MODEL_PRICES is not valid; ignored", error=f"{e.__class__.__name__}: {e}")
        return {}


OPENROUTER_MODELS = "https://openrouter.ai/api/v1/models"
#: refresh OpenRouter prices this often
REFRESH_S = 86_400


@dataclass(frozen=True)
class Rate:
    """USD per token."""

    input: float
    output: float
    cached: float | None = None


@dataclass(frozen=True)
class CatalogModel:
    """One OpenRouter model, as needed to suggest cheaper ones."""

    id: str
    name: str
    rate: Rate
    context: int
    #: can call tools / return structured output (every yachat job needs this)
    tools: bool
    audio_in: bool


#: OpenRouter prices by normalized model name, and when they were fetched (0: never)
_openrouter: dict[str, Rate] = {}
_catalog: list[CatalogModel] = []
_openrouter_at: float = 0.0


def _norm(name: str) -> str:
    """'claude-opus-5.5' and 'claude-opus-5-5' are one model; case and vendor prefixes don't matter."""
    return re.sub(r"[._]", "-", name.lower().rsplit("/", 1)[-1])


def load_openrouter(models: list[dict[str, Any]]) -> int:
    """Index OpenRouter's /models list by model name (variants like ':batch' and aliases like '~latest' skipped)."""
    global _openrouter_at
    table: dict[str, Rate] = {}
    catalog: list[CatalogModel] = []
    for m in models:
        mid, p = str(m.get("id", "")), m.get("pricing") or {}
        if not mid or ":" in mid or mid.startswith("~"):
            continue
        try:
            rate = Rate(float(p["prompt"]), float(p["completion"]), float(p["input_cache_read"]) if p.get("input_cache_read") else None)
        except (KeyError, TypeError, ValueError):
            continue
        if rate.input < 0 or rate.output < 0:  # "-1": price varies by route
            continue
        table.setdefault(_norm(mid), rate)
        params = set(m.get("supported_parameters") or [])
        arch = m.get("architecture") or {}
        catalog.append(
            CatalogModel(
                id=mid,
                name=str(m.get("name") or mid),
                rate=rate,
                context=int(m.get("context_length") or 0),
                tools="tools" in params or "structured_outputs" in params,
                audio_in="audio" in (arch.get("input_modalities") or []),
            )
        )
    _openrouter.clear()
    _openrouter.update(table)
    _catalog[:] = catalog
    _openrouter_at = time.time() if table else 0.0
    return len(table)


async def refresh_openrouter() -> int:
    """Fetch OpenRouter's current prices (public, no key). Keeps the last good list when the fetch fails."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(OPENROUTER_MODELS)
            r.raise_for_status()
            n = load_openrouter(list(r.json().get("data") or []))
    except (httpx.HTTPError, ValueError) as e:
        log.error("OpenRouter prices not refreshed; keeping the last list", error=f"{e.__class__.__name__}: {e}", models=len(_openrouter))
        return 0
    log.info("OpenRouter prices refreshed", models=n)
    return n


def openrouter_rate(*names: str | None) -> Rate | None:
    for name in names:
        if name and (rate := _openrouter.get(_norm(name))) is not None:
            return rate
    return None


#: what each model job does, by the purpose of its calls (everything else is the generate job)
CLASSIFY_PURPOSES = frozenset({"intent_detect", "affirm", "expect", "shorthand", "survey_assess", "survey_interpret", "memory_extract"})
VERIFY_PURPOSES = frozenset({"reply_verify", "reasoning_verify", "verify_grounded"})


#: suggestions come from established model makers only (a cheap unknown model is rarely a saving once quality counts)
PROVIDERS = frozenset({"google", "openai", "anthropic", "meta-llama", "qwen", "deepseek", "mistralai", "x-ai", "moonshotai", "z-ai", "minimax", "amazon"})


def job_of(purpose: str) -> str:
    return "classify" if purpose in CLASSIFY_PURPOSES else "verify" if purpose in VERIFY_PURPOSES else "generate"


def cheaper(current: str, input_tokens: int, output_tokens: int, *, min_context: int = 32_000, audio: bool = False, n: int = 6) -> list[dict[str, Any]]:
    """OpenRouter models that can do the job and would have cost less on this workspace's own token mix, cheapest first.
    Price is the only thing compared: check quality with a simulation before switching."""
    now_rate = openrouter_rate(current)
    tokens_in, tokens_out = max(input_tokens, 1), max(output_tokens, 0)

    def cost(r: Rate) -> float:
        return tokens_in * r.input + tokens_out * r.output

    baseline = cost(now_rate) if now_rate else None
    out = []
    for m in _catalog:
        maker = m.id.split("/", 1)[0]
        if maker not in PROVIDERS or not m.tools or m.context < min_context or (audio and not m.audio_in) or m.rate.input <= 0 or _norm(m.id) == _norm(current):
            continue
        c = cost(m.rate)
        if baseline is not None and c >= baseline:
            continue
        out.append(
            {
                "model": f"openrouter/{m.id}",
                "name": m.name,
                "input_per_mtok": round(m.rate.input * 1e6, 4),
                "output_per_mtok": round(m.rate.output * 1e6, 4),
                "context": m.context,
                "cost_usd": round(c, 6),
                "saving": round(1 - c / baseline, 3) if baseline else None,
            }
        )
    # prefer known families first among near-equal prices: sort by cost, then name
    return sorted(out, key=lambda x: (x["cost_usd"], x["name"]))[:n]


def prices_fetched_at() -> float | None:
    return _openrouter_at or None


def price(model: str, input_tokens: int, output_tokens: int, provider_model: str | None = None, cached_tokens: int = 0) -> tuple[float | None, str]:
    """(cost in USD, where the price came from: "config", "openrouter", "litellm" or "unknown")."""
    for name in (model, provider_model):
        if name and name in price_overrides():
            pin, pout = price_overrides()[name]
            return round((input_tokens * pin + output_tokens * pout) / 1_000_000, 8), "config"
    if (rate := openrouter_rate(model, provider_model)) is not None:
        cached = min(cached_tokens, input_tokens) if rate.cached is not None else 0
        cost = (input_tokens - cached) * rate.input + cached * (rate.cached or 0.0) + output_tokens * rate.output
        return round(cost, 8), "openrouter"
    try:
        import litellm

        for name in (provider_model, model):
            if not name:
                continue
            try:
                cin, cout = litellm.cost_per_token(model=name, prompt_tokens=input_tokens, completion_tokens=output_tokens)
                return round(float(cin) + float(cout), 8), "litellm"
            except Exception:  # not in litellm's price list under this name
                continue
    except ImportError:  # pragma: no cover
        pass
    return None, "unknown"


def record(
    model: str, purpose: str, input_tokens: int, output_tokens: int, *, cached_tokens: int = 0, ms: float = 0.0, provider_model: str | None = None
) -> None:
    """Called by the LLM clients after each successful call."""
    meter = METER.get()
    if meter is None:
        return
    cost, _ = price(model, input_tokens, output_tokens, provider_model, cached_tokens)
    meter.append(Usage(model, purpose, input_tokens, output_tokens, cached_tokens, round(ms, 1), cost))


def usage_of(resp: Any) -> tuple[int, int, int]:
    """(input, output, cached) tokens from an Anthropic or OpenAI-style (litellm) response; zeros when absent."""
    u = getattr(resp, "usage", None)
    if u is None:
        return 0, 0, 0
    if hasattr(u, "input_tokens"):  # Anthropic: input_tokens excludes cached reads and cache writes
        cached = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        written = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        return int(u.input_tokens or 0) + cached + written, int(u.output_tokens or 0), cached
    details = getattr(u, "prompt_tokens_details", None)
    cached = int(getattr(details, "cached_tokens", 0) or 0) if details is not None else 0
    return int(getattr(u, "prompt_tokens", 0) or 0), int(getattr(u, "completion_tokens", 0) or 0), cached

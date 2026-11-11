"""Model usage and cost. Each LLM client's provider call is wrapped in `@metered`, which records the call (model,
purpose, tokens, time) to the meter of the turn it runs in; the runtime stores them in the tenant's audit log.

Prices come from KEELCHAT_MODEL_PRICES, else the cost a LiteLLM gateway reports for the call, else OpenRouter's public
model list (refreshed daily), else litellm's price list. Behind a gateway, the model it actually routed to is priced,
not its alias. A Headroom proxy's saved tokens are recorded too. A model with no known price is recorded with tokens
and no cost."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache, wraps
from typing import TYPE_CHECKING, Any, cast

from keelchat.logging import get_logger

if TYPE_CHECKING:
    from keelchat.runtime.llm import LLMRequest

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
    #: input tokens a Headroom proxy removed before the call (not billed)
    saved_tokens: int = 0


#: the reply when a tenant's spending limit is reached (no model is called)
LIMIT_REACHED = "This assistant has reached its usage limit for now. Please try again later."


def unpriced_rate() -> float:
    """USD per million tokens charged against a limit for a call with no known price, so an unpriced model is never free."""
    return float(os.getenv("KEELCHAT_UNPRICED_USD_PER_MTOK") or 15.0)


def charge_usd(calls: list[Usage]) -> float:
    """What the calls count against a spending limit: their price, or the unpriced rate when the price is unknown."""
    return sum(c.cost_usd if c.cost_usd is not None else (c.input_tokens + c.output_tokens) * unpriced_rate() / 1_000_000 for c in calls)


#: the calls of the turn (or background task) running now; None outside a turn (nothing is recorded)
METER: ContextVar[list[Usage] | None] = ContextVar("usage_meter", default=None)


@lru_cache(maxsize=1)
def price_overrides() -> dict[str, tuple[float, float]]:
    """KEELCHAT_MODEL_PRICES='{"claude-opus-5-5": [5, 25]}': USD per million input and output tokens."""
    raw = os.getenv("KEELCHAT_MODEL_PRICES") or ""
    if not raw.strip():
        return {}
    try:
        return {str(k): (float(v[0]), float(v[1])) for k, v in json.loads(raw).items()}
    except (ValueError, TypeError, IndexError, AttributeError) as e:
        log.error("KEELCHAT_MODEL_PRICES is not valid; ignored", error=f"{e.__class__.__name__}: {e}")
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
    #: can call tools / return structured output (every keelchat job needs this)
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
    model: str,
    purpose: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cached_tokens: int = 0,
    ms: float = 0.0,
    provider_model: str | None = None,
    reported_cost: float | None = None,
    saved_tokens: int = 0,
) -> None:
    """One successful model call, on the meter of the turn running now. `reported_cost` is what a gateway billed; it is
    used unless KEELCHAT_MODEL_PRICES names the model. Prefer `@metered`, which calls this."""
    meter = METER.get()
    if meter is None:
        return
    if reported_cost is not None and not any(n in price_overrides() for n in (model, provider_model) if n):
        cost: float | None = round(reported_cost, 8)
    else:
        cost, _ = price(model, input_tokens, output_tokens, provider_model, cached_tokens)
    meter.append(Usage(model, purpose, input_tokens, output_tokens, cached_tokens, round(ms, 1), cost, saved_tokens))


def add(calls: list[Usage]) -> None:
    """Calls another service made and priced for this turn (the Analytics Service), added to its meter."""
    meter = METER.get()
    if meter is not None:
        meter.extend(calls)


@contextmanager
def metering() -> Iterator[list[Usage]]:
    """Collect the model calls made inside the block (a service's own request), whatever turn is running."""
    calls: list[Usage] = []
    token = METER.set(calls)
    try:
        yield calls
    finally:
        METER.reset(token)


def proxy_report(resp: Any) -> tuple[str | None, float | None, int]:
    """(model routed to, cost billed, tokens saved) from the headers a LiteLLM gateway and a Headroom proxy add to a
    response, as litellm exposes them. A cost of 0 is "not priced by the gateway", not free."""
    hidden = getattr(resp, "_hidden_params", None)
    headers = (hidden.get("additional_headers") if isinstance(hidden, dict) else None) or {}

    def header(name: str) -> str | None:
        v = headers.get("llm_provider-" + name)
        return str(v) if v not in (None, "") else None

    routed = header("x-litellm-model-name")
    try:
        cost = float(header("x-litellm-response-cost") or header("x-litellm-response-cost-original") or 0) or None
    except ValueError:
        cost = None
    try:
        saved = max(0, int(header("x-headroom-tokens-saved") or 0))
    except ValueError:
        saved = 0
    return routed, cost, saved


def metered[F: Callable[..., Awaitable[Any]]](provider_model: Callable[[LLMRequest], str | None] | None = None) -> Callable[[F], F]:
    """Decorate an LLM client's provider call, `async def _send(self, req, ...) -> response`: each successful call is
    recorded with its tokens in and out (Anthropic or OpenAI-style usage), time and cost. Behind a LiteLLM gateway or a
    Headroom proxy it records the routed model, the gateway's cost and the tokens saved. `provider_model(req)` names the
    model to price when the request's model is an alias. A custom `LLMClient` gets cost tracking from this alone."""

    def wrap(call: F) -> F:
        @wraps(call)
        async def run(self: Any, req: LLMRequest, *args: Any, **kwargs: Any) -> Any:
            t0 = time.perf_counter()
            resp = await call(self, req, *args, **kwargs)
            tin, tout, cached = usage_of(resp)
            routed, cost, saved = proxy_report(resp)
            record(
                req.model,
                req.purpose,
                tin,
                tout,
                cached_tokens=cached,
                ms=(time.perf_counter() - t0) * 1000,
                provider_model=routed or (provider_model(req) if provider_model else None),
                reported_cost=cost,
                saved_tokens=saved,
            )
            return resp

        return cast(F, run)

    return wrap


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

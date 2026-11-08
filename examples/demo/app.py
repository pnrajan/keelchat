"""The public demo: a landing page where visitors describe a bot and get its YAML, then clone the repo to run it, and a
ready-made support assistant to chat with.

Everything is in memory, so it is set up again on every start; the fixed id keeps the chat link the same.
Run it with `uvicorn examples.demo.app:create_app --factory`. Deployed with deploy/demo/fly.toml (scripts/demo.sh).
"""

from __future__ import annotations

import asyncio
import html
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from yachat.api.app import create_app as create_api
from yachat.api.public import client_ip
from yachat.logging import get_logger
from yachat.policy.compiler import build
from yachat.policy.drafter import draft_policy, edit_policy
from yachat.policy.errors import PolicyValidationError
from yachat.runtime.llm import LLMError
from yachat.runtime.runtime import Runtime
from yachat.state.models import Embed
from yachat.state.store import InMemoryEmbedStore

from .needs import Need, from_policy, suggested

HERE = Path(__file__).parent
POLICY = HERE.parent / "support_assistant.yaml"
DEMO_ID = os.getenv("YACHAT_DEMO_ID", "pk_demo_support")
#: drafts per visitor network per 10 minutes, and per day across everyone: each one is several model calls
DRAFTS_PER_IP = int(os.getenv("YACHAT_DEMO_DRAFTS_PER_IP", "6"))
DRAFTS_PER_DAY = int(os.getenv("YACHAT_DEMO_DRAFTS_DAILY", "200"))
log = get_logger("demo")


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: what the bot should do, or the change to make to `yaml`
    text: str = Field(min_length=3, max_length=1_000)
    #: the visitor's current draft, when asking for a change
    yaml: str = Field(default="", max_length=20_000)


def create_app() -> FastAPI:
    embeds = InMemoryEmbedStore()
    app = create_api(embeds=embeds)
    rt: Runtime = app.state.runtime
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[None]:
        async with inner(a):
            yaml = POLICY.read_text()
            policy = build(yaml)
            await rt.save_version(policy, yaml, note="demo")
            await embeds.put(
                Embed(
                    id=DEMO_ID,
                    tenant=policy.tenant,
                    agent=policy.agent,
                    name="yachat demo",
                    title="yachat demo: support assistant",
                    daily_limit=int(os.getenv("YACHAT_DEMO_DAILY", "300")),
                )
            )
            yield

    app.router.lifespan_context = lifespan

    async def draft(body: DraftIn, request: Request) -> dict[str, object]:
        """A policy from plain words, or the visitor's draft changed as asked. Checked by the build; never saved."""
        if not await rt.limits.take(f"draft-ip:{client_ip(request)}", DRAFTS_PER_IP, 600):
            raise HTTPException(429, "That's a lot of bots. Wait a few minutes and try again.")
        if not await rt.limits.take("draft-day", DRAFTS_PER_DAY, 86_400):
            raise HTTPException(429, "The demo has made all the bots it can today. Clone the repo to keep going.")
        if body.yaml.strip():
            d, _ = await edit_policy(rt.llm, rt.models, body.yaml, body.text, "default", {})
            extra: list[Need] | None = None  # the page keeps the suggestions from the first draft
        else:
            d, extra = await asyncio.gather(draft_policy(rt.llm, rt.models, body.text, "default", {}), suggest(body.text))
        return {
            "yaml": d.yaml,
            "errors": [{"path": e.path, "message": e.message} for e in d.errors],
            "needs": [n.model_dump() for n in uses(d.yaml)],
            "suggested": None if extra is None else [n.model_dump() for n in extra],
        }

    async def suggest(description: str) -> list[Need]:
        try:
            return await suggested(rt.llm, rt.models, description, "")
        except LLMError as e:  # the draft still arrives; the page says suggestions are unavailable
            log.error("needs suggestion failed", error=str(e)[:300])
            return []

    def uses(yaml: str) -> list[Need]:
        try:
            return from_policy(build(yaml))
        except PolicyValidationError:
            return []

    page = (HERE / "index.html").read_text().replace("__EMBED_ID__", DEMO_ID).replace("__POLICY__", html.escape(POLICY.read_text()))
    script = (HERE / "app.js").read_text()  # its own file: the app's CSP blocks inline scripts
    # ahead of the API's own routes: "/" is the landing page here (the console needs the key)
    app.router.routes[:0] = [
        APIRoute("/", lambda: HTMLResponse(page), include_in_schema=False),
        APIRoute("/demo/app.js", lambda: Response(script, media_type="text/javascript"), include_in_schema=False),
        APIRoute("/demo/draft", draft, methods=["POST"], include_in_schema=False),
    ]
    return app

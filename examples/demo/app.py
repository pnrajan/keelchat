"""The public demo: a landing page where visitors describe a bot, get its YAML and chat with it right away (tools on
sample data), then clone the repo to run it; and a ready-made support assistant to chat with.

Everything is in memory, so it is set up again on every start; the fixed id keeps the chat link the same.
Run it with `uvicorn examples.demo.app:create_app --factory`. Deployed with deploy/demo/fly.toml (scripts/demo.sh).
"""

from __future__ import annotations

import asyncio
import html
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field

from keelchat.api.app import create_app as create_api
from keelchat.api.public import client_ip
from keelchat.logging import get_logger
from keelchat.policy.compiler import build
from keelchat.policy.drafter import Option, Question, clarify, draft_policy, edit_policy, with_answers
from keelchat.policy.errors import PolicyValidationError
from keelchat.runtime.llm import LLMError
from keelchat.runtime.runtime import Runtime
from keelchat.state.models import Embed
from keelchat.state.store import InMemoryEmbedStore

from .needs import Need, from_policy, suggested

HERE = Path(__file__).parent
POLICY = HERE.parent / "support_assistant.yaml"
DEMO_ID = os.getenv("KEELCHAT_DEMO_ID", "pk_demo_support")
#: drafts per visitor network per 10 minutes, and per day across everyone: each one is several model calls
DRAFTS_PER_IP = int(os.getenv("KEELCHAT_DEMO_DRAFTS_PER_IP", "6"))
DRAFTS_PER_DAY = int(os.getenv("KEELCHAT_DEMO_DRAFTS_DAILY", "200"))
#: drafted bots put up to chat with, the same way; each is kept in memory until the machine restarts
TRIES_PER_IP = int(os.getenv("KEELCHAT_DEMO_TRIES_PER_IP", "10"))
TRIES_PER_DAY = int(os.getenv("KEELCHAT_DEMO_TRIES_DAILY", "300"))
#: conversations per tried bot per day
TRY_CHATS = 30
#: a stronger model for writing bots than for chatting (YAML that builds, handoffs in the right place); unset: the
#: chat's own KEELCHAT_MODEL_GENERATE
DRAFT_MODEL = os.getenv("KEELCHAT_DEMO_DRAFT_MODEL", "")
log = get_logger("demo")


class OptionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(max_length=60)
    adds: str = Field(max_length=300)


class QuestionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(max_length=40)
    ask: str = Field(max_length=200)
    why: str = Field(default="", max_length=200)
    multi: bool = False
    options: list[OptionIn] = Field(max_length=5)


class ClarifyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: what the bot should do
    text: str = Field(min_length=3, max_length=1_000)


class DraftIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: what the bot should do, or the change to make to `yaml`
    text: str = Field(min_length=3, max_length=1_000)
    #: the visitor's current draft, when asking for a change
    yaml: str = Field(default="", max_length=20_000)
    #: the questions /demo/clarify asked, and the options picked (question id → labels, or the visitor's own words)
    questions: list[QuestionIn] = Field(default_factory=list, max_length=5)
    answers: dict[str, list[str]] = Field(default_factory=dict, max_length=5)


class TryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: the visitor's draft
    yaml: str = Field(min_length=10, max_length=20_000)


def create_app() -> FastAPI:
    embeds = InMemoryEmbedStore()
    app = create_api(embeds=embeds)
    rt: Runtime = app.state.runtime
    drafting = replace(rt.models, generate=DRAFT_MODEL) if DRAFT_MODEL else rt.models
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
                    name="keelchat demo",
                    title="keelchat demo: support assistant",
                    daily_limit=int(os.getenv("KEELCHAT_DEMO_DAILY", "300")),
                )
            )
            yield

    app.router.lifespan_context = lifespan

    async def clarifying(body: ClarifyIn, request: Request) -> dict[str, object]:
        """Questions to click before the draft, from the drafting model. Free of the draft limits but rate-limited."""
        if not await rt.limits.take(f"clarify-ip:{client_ip(request)}", DRAFTS_PER_IP, 600):
            raise HTTPException(429, "That's a lot of bots. Wait a few minutes and try again.")
        try:
            questions = await clarify(rt.llm, drafting, body.text)
        except LLMError as e:  # no questions: the page drafts straight away
            log.error("clarifying questions failed", error=str(e)[:300])
            questions = []
        return {"questions": [{"id": q.id, "ask": q.ask, "why": q.why, "multi": q.multi, "options": [o.__dict__ for o in q.options]} for q in questions]}

    async def draft(body: DraftIn, request: Request) -> dict[str, object]:
        """A policy from plain words, or the visitor's draft changed as asked. Checked by the build; never saved."""
        if not await rt.limits.take(f"draft-ip:{client_ip(request)}", DRAFTS_PER_IP, 600):
            raise HTTPException(429, "That's a lot of bots. Wait a few minutes and try again.")
        if not await rt.limits.take("draft-day", DRAFTS_PER_DAY, 86_400):
            raise HTTPException(429, "The demo has made all the bots it can today. Clone the repo to keep going.")
        if body.yaml.strip():
            d, _ = await edit_policy(rt.llm, drafting, body.yaml, body.text, "default", {})
            extra: list[Need] | None = None  # the page keeps the suggestions from the first draft
        else:
            asked = [Question(q.id, q.ask, q.why, q.multi, [Option(o.label, o.adds) for o in q.options]) for q in body.questions]
            described = with_answers(body.text, {k: v[:5] for k, v in body.answers.items()}, asked)
            d, extra = await asyncio.gather(draft_policy(rt.llm, drafting, described, "default", {}), suggest(described))
        return {
            "yaml": d.yaml,
            "errors": [{"path": e.path, "message": e.message} for e in d.errors],
            "needs": [n.model_dump() for n in uses(d.yaml)],
            "suggested": None if extra is None else [n.model_dump() for n in extra],
        }

    async def try_it(body: TryIn, request: Request) -> dict[str, object]:
        """Put the visitor's draft up to chat with: its own tenant (nobody else's bot), a new embed id, few chats."""
        try:
            policy = build(body.yaml, tenant="try-" + secrets.token_hex(8))
        except PolicyValidationError as e:
            raise HTTPException(422, "The YAML doesn't build yet: " + "; ".join(f"{x.path}: {x.message}" for x in e.errors[:3])) from e
        if not await rt.limits.take(f"try-ip:{client_ip(request)}", TRIES_PER_IP, 600):
            raise HTTPException(429, "That's a lot of bots. Wait a few minutes and try again.")
        if not await rt.limits.take("try-day", TRIES_PER_DAY, 86_400):
            raise HTTPException(429, "The demo has run all the bots it can today. Clone the repo to keep going.")
        await rt.save_version(policy, body.yaml, note="demo try")
        embed = Embed(tenant=policy.tenant, agent=policy.agent, name="keelchat demo: your bot", daily_limit=TRY_CHATS)
        await embeds.put(embed)
        log.info("demo bot up", agent=policy.agent, policy_hash=policy.hash)
        return {"embed": embed.id, "agent": policy.agent}

    async def suggest(description: str) -> list[Need]:
        try:
            return await suggested(rt.llm, drafting, description, "")
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
        APIRoute("/demo/clarify", clarifying, methods=["POST"], include_in_schema=False),
        APIRoute("/demo/draft", draft, methods=["POST"], include_in_schema=False),
        APIRoute("/demo/try", try_it, methods=["POST"], include_in_schema=False),
    ]
    return app

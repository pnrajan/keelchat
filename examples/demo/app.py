"""The public demo: yachat's API with the support assistant saved and published as a chat page at a fixed address.

Everything is in memory, so it is set up again on every start; the fixed id keeps the link the same.
Run it with `uvicorn examples.demo.app:create_app --factory`. Deployed with deploy/demo/fly.toml.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute

from yachat.api.app import create_app as create_api
from yachat.policy.compiler import build
from yachat.state.models import Embed
from yachat.state.store import InMemoryEmbedStore

POLICY = Path(__file__).parents[1] / "support_assistant.yaml"
DEMO_ID = os.getenv("YACHAT_DEMO_ID", "pk_demo_support")


def create_app() -> FastAPI:
    embeds = InMemoryEmbedStore()
    app = create_api(embeds=embeds)
    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: FastAPI) -> AsyncIterator[None]:
        async with inner(a):
            yaml = POLICY.read_text()
            policy = build(yaml)
            await app.state.runtime.save_version(policy, yaml, note="demo")
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
    # visitors land on the chat; the console needs the key
    app.router.routes.insert(0, APIRoute("/", lambda: RedirectResponse(f"/a/{DEMO_ID}"), include_in_schema=False))
    return app

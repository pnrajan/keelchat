---
name: verify
description: Run keelchat's services locally and drive them (HTTP + console) to verify a change at its surface.
---

# Verify keelchat at runtime

Tests call the real model; verification means running the services and driving them.

## Start (macOS, no Docker Compose needed)
- Write a launcher script to a scratch dir and run it in the background; long `&&` chains in the Bash tool get
  rewritten by the rtk hook. Unset `KEELCHAT_API_KEY`, `DATABASE_URL`, `REDIS_URL` for an in-process run
  (the local `.env` DATABASE_URL may point at a database that does not exist).
- API: `uv run uvicorn keelchat.api.app:create_app --factory --port 8000` (serves `frontend/dist` once built).
- Knowledge: `uv run keelchat knowledge serve --port 8001`; set `KNOWLEDGE_URL` on the API.
- Analytics: `uv run keelchat analytics serve --port 8002`; set `ANALYTICS_URL` on the API and
  `KEELCHAT_DATA_ROOT` on both. Sandbox: `KEELCHAT_SANDBOX=local KEELCHAT_SANDBOX_BACKEND=docker`
  (build `deploy/sandbox` as `keelchat-sandbox:latest` first) or `KEELCHAT_SANDBOX=agentcore` (AWS creds in .env).
- Or `scripts/dev.sh --analytics [--sandbox] --build`: starts both and registers `examples/data` as dataset `sales`.

## Drive
- `PUT /v1/tenants/{t}/datasets/{name}`, then `POST /v1/agents {yaml, tenant}`, `POST /v1/sessions`,
  `POST /v1/sessions/{id}/turns`. `examples/sales_analyst.yaml` is the analytics agent; its known answers:
  US 30,990 / APAC 14,620 / EU 10,450 revenue; EU monthly trend p≈0.0086.
- Console: http://localhost:8000/console/ (landing page at /) — templates dropdown, Build & try, New session, Analysis tab.
- Bubblewrap sandbox only runs on Linux; in Docker it needs `--security-opt systempaths=unconfined
  --security-opt seccomp=unconfined`.

## Gotchas
- SiliconFlow rate-limits (TPM) when the test suite runs in parallel; analytics then degrades to `partial`. Verify
  live flows when the suite is not running.
- An analysis turn takes 10-60 s (several model calls); wait on the reply, not a fixed sleep.

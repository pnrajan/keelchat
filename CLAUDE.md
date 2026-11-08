# CLAUDE.md — yachat

Engineering constitution. These rules **override defaults**. When a change conflicts with a rule
here, stop and flag it — don't work around it. Detail lives one level down (§5); read the relevant
file before working in that area.

---

## 0. How to work — read first

Bias toward **caution over speed**; for trivial tasks use judgment.

- **Think before coding.** Don't assume, don't hide confusion, surface tradeoffs. State
  assumptions; ask if uncertain. If interpretations differ, present them — don't pick silently. If
  a simpler approach exists, say so and push back when warranted.
- **Simplicity first.** Minimum code that solves the problem. No speculative features, single-use
  abstractions, unrequested "flexibility", or error handling for impossible cases. Self-check:
  *would a senior engineer call this overcomplicated?* (Exception: the seams in §2 — registries,
  stage interface, store/LLM/tool/channel ports — are required; simplicity applies *inside* them.)
- **Surgical changes.** Touch only what the request needs. Don't improve/refactor/reformat adjacent
  code. Match existing style. Remove only orphans *your* change created; flag pre-existing dead
  code, don't delete it. Every changed line traces to the request.
- **Reuse over rebuild.** Don't hand-write what a maintained library does well (HTTP, parsing,
  retries, crypto). Add the dependency, write thin glue. Never reimplement security primitives.
- **Goal-driven.** Turn tasks into verifiable goals — "fix bug" → *write a failing test, make it
  pass*. For multi-step work state `1. [step] → verify: [check]` and loop until green.
- **Scope discipline.** Do not add features beyond the request; the laws in §2 and the components in
  `docs/ARCHITECTURE.md` are the boundary. When something is ambiguous, pick the simplest option and say so in
  the PR.

Working if: no unnecessary lines in diffs, no rewrites for overcomplication, questions arrive
*before* implementation.

---

## 1. What / why

Conversational agents defined by **one short YAML** (`agent.yaml`: agent, charter, languages, tools, flow, guardrails, limits,
memory, mode) and run on **one shared runtime**. The **registry** (code, auto-discovered) holds every capability:
tools, nodes, guardrails. A new customer agent is a new YAML file; a new capability is one decorated function or
class, from any team, without touching the runtime.

---

## 2. The architectural laws

1. **The policy is the only product surface.** Behaviour comes from `CompiledPolicy`; the runtime never branches on
   an agent name. Scope and tone live only in the charter text.
2. **Three stages, sub-steps inside.** Understand → Decide → Respond. Every step is
   `async def run(ctx: TurnContext) -> TurnContext`; steps share only `TurnContext`; short-circuit jumps to Respond.
3. **Every external dependency sits behind a port with an in-memory fake**: `LLMClient`, `SessionStore`,
   `MemoryStore`, each tool, each channel, embedder, document store, vector index. **The model is the exception: there
   is no fake LLM; tests, sims and evals call the configured provider** (`runtime/llm_factory.py`). Composition
   happens in `yachat/runtime/runtime.py`, `yachat/knowledge/service.py:service_from_env` and
   `yachat/analytics/service.py:service_from_env`. Model-written code runs only behind the `Sandbox` port.
4. **Tools are reached only through the Tool Gateway**, which enforces the allow list, timeouts, the
   circuit breaker and scrubs sensitive values. Tool output is untrusted data in prompts.
5. **Counting lives in code.** Digressions, re-asks, nudges, reopens, planner budget: the runtime increments from
   structured verdicts; a model never decides to exit.
6. **Channels own their limits and compliance.** The Response Generator emits a channel-neutral message.
7. **Compiled policies are immutable and pinned per session**, defaults included.
8. **Memory is opt-in per item and privacy-ruled**: policy-listed items only, sensitive items need consent, never to
   tools, never from flagged turns, deletable per user.

---

## 3. Stack & verification

Python 3.12, FastAPI, Pydantic v2, asyncio, `uv`, pytest + pytest-asyncio + pytest-xdist (tests call the real model in
`.env`), `ruff`, `mypy --strict` on
`yachat/`. Claude via the Anthropic SDK behind `LLMClient` (`runtime/llm.py`); litellm for other providers and
SiliconFlow embeddings. Redis (sessions), Postgres (memory), S3 + LanceDB (knowledge) as optional adapters.
React + Vite test console in `frontend/`.

A change is verified when `uv run pytest`, `uv run ruff check .` and `uv run mypy` pass, and for the console
`npm run build`. Capability changes must also pass `tests/test_registry.py`. **Linters own code style.**

---

## 4. Structure

```
yachat/
  policy/        models, loader, validator (YAML paths), compiler (CompiledPolicy), defaults, memory_types, durations, errors,
                 schema (editor JSON Schema), drafter (policy from a description)
  registry/      core (@tool/@action/@node/@guardrail/@preprocessor, REGISTRY, manifest), discovery, contract,
                 mcp (MCP servers → `<alias>.<tool>`), tenant_tools (Tool Registry API HTTP tools)
  runtime/       context (TurnContext), pipeline (stages, timing, short-circuit), llm (Anthropic), litellm_client,
                 understand/  stage (runs registered pre-processors in order), guardrails runner, language_rules
    decide/      intent_detector (intent lists, few-shots from intent_examples.yaml, on-step enquiries), orchestrator
                 (flow/planner/adaptive, edits, follow-ups), planner, tool_gateway (+ActionRunner); redaction.py for logs
    respond/     rewriter (goal → reply, per-step verifier), guardrails, response_generator, channel
                 runtime.py (composition: lock, dedupe, pinning, async memory task), audit.py (decision log, dashboard), usage.py (tokens, cost), findings.py (survey findings)
  capabilities/  tools.py (+actions), guardrails.py, nodes/ (greet_consent, job_search, form_fill, survey, conversation_survey (objectives), knowledge, analytics, exit, handoff),
                 preprocessors/ (receiver, opt_out, handoff_request, language, shorthand, entities, input_guardrails)
  channels/      base, web, slack, sms (STOP), whatsapp (list messages), whatsapp_cloud (Cloud API)
  state/         models (SessionState, MemoryRecord, PolicyVersion, AuditEvent, Handoff), store (ports + fakes; saved policy
                 versions, audit log, handoff inbox), flow
                 (StepGraph), memory_writer, redis_store, postgres_store (memory, tenant tools, datasets, versions, audit, handoffs, embeds), iceberg_store (audit archive), mem0_store (memory),
                 artifacts (file store port: S3, folder, memory; uploads, documents)
  reasoning/     verified reasoning: plan + checks before the work, a verifier after, rounds counted in code
  datasets/      models (Dataset: structured | unstructured, StructuredRef/UnstructuredRef), sources (roots, reads), store
  analytics/     models (AnalysisResult), duck (locked DuckDB), vanna_sql, analyst, grounding, code_agent (smolagents),
                 sandbox (port: llm-sandbox | AgentCore), service, api (standalone microservice: /datasets, /analyze)
  knowledge/     models, chunker (semantic), embedder (SiliconFlow/fake), docstore (S3/fake), vectorstore (LanceDB/fake), strategies, service,
                 api (standalone microservice: /documents, /search)
  api/app.py     FastAPI; serves frontend/dist when built; tenant keys; mcp_server.py (/mcp for Claude and MCP clients)
  sim/harness.py simulated users: scripted replays and model-played personas, on fake tools; sim/refine.py (survey self-test: pretend customers, confidence score, revise)
  testing/fakes.py  FakeTools (tool backends only; the model is never faked)
  scaffold.py    `yachat new agent|package` starter files; __init__.py re-exports the extension API (`from yachat import tool, node, ...`)
  logging.py     JSON logs (session_id, turn_id, policy_hash, stage, latency_ms), Metrics; tracing.py (Sentry spans); voice.py (speech in/out ports)
  evals/         replay (scripts + played personas on sim/harness), metrics (DeepEval, Ragas, rule_followed, thresholds gate), judge (litellm), run, online
evals/           <agent>/ sets: replay.yaml (fixtures, cases, expect), thresholds.yaml (policy, minimums, rubrics)
examples/        policies (support, booking, concierge, hiring, surveys, analyst, coach), data/, sim_script.yaml, contrib_tool/,
                 extensions/booking_caps (example tool, guardrail and channel package)
frontend/        React test console (policy editor, chat, trace/state inspector, simulate, versions, channels, results, handoff inbox)
plugins/yachat/  Claude Code plugin (.mcp.json → /mcp, build-agent skill); .claude-plugin/marketplace.json lists it
docs/            DEVELOPER_GUIDE.md, ARCHITECTURE.md, WRITING_AN_AGENT.md, ADDING_A_CAPABILITY.md, EVALS.md
scripts/        dev.sh, knowledge.sh, analytics.sh, db.sh (docker compose Redis+Postgres); run.sh at the root;
                 Dockerfile, docker-compose.yml; deploy/sandbox (sandbox image)
tests/           helpers.py (make_runtime/say/reach_form), one file per area
```

New capability → decorate, drop it under `capabilities/` or ship an entry point, pass the contract suite.
See `docs/ADDING_A_CAPABILITY.md`.

---

## 5. Where the detail lives

| File | Covers |
|---|---|
| `docs/BUILDING_AN_AGENT_END_TO_END.md` | one complex agent from plan to launch, in the console, the API and Claude |
| `docs/DEVELOPER_GUIDE.md` | run modes, an API walkthrough, where code lives, debugging and troubleshooting |
| `docs/ADDING_A_CAPABILITY.md` | how another team registers a node/tool/guardrail/channel |
| `README.md` | what yachat is, quickstart, one example, links into the docs (keep it short) |
| `docs/FEATURES.md` | the console, channels, routing, memory, handoff, documents, data, testing, Claude |
| `docs/REFERENCE.md` | CLI, HTTP API, keys, model routing and cost, dashboards, env vars, deployment, troubleshooting |
| `docs/COMPARISON.md` | yachat against Rasa Pro, Parlant, LangGraph and Botpress |
| `docs/ARCHITECTURE.md` | components, life of a message, deployment topology |
| `docs/WRITING_AN_AGENT.md` | how a team writes a goal-oriented policy; pre-launch checklist |
| `CONTRIBUTING.md`, `SECURITY.md` | PR checks and review rules; vulnerability reporting and runtime protections |

---

## 6. Correctness (must)

- **Types everywhere**; no `Any` or untyped dicts crossing layers — except tool payloads and
  LLM JSON, converted to a typed model the moment they enter runtime code.
- **Validate at every boundary** — YAML via Pydantic (`extra="forbid"`), capability config via
  the capability's registered schema, HTTP via Pydantic. Reject bad input loudly with the YAML
  path; never coerce silently.
- **Typed, explicit errors** (`PolicyValidationError`, `ToolError`, `LLMError`, `VersionConflict`, `PolicyRetired`). No bare
  `except`, no silent fallbacks. Vendor error text is logged, never shown to the user.
- **Structured logs** carry `session_id`, `turn_id`, `policy_hash`, `stage` on every line.
- **Model ids and keys come from config**, never hardcoded in a stage. A selected provider with a
  missing key raises; nothing degrades to a fake model. Tool/store fakes run offline.
- **Never store guardrail-flagged content in user memory.**

---

## 7. Writing future instructions & docs (standing convention)

- **Lean root, progressive disclosure.** Keep this file universal and short; push specialized
  detail into a scoped doc behind a one-line pointer.
- **Cover WHY / WHAT / HOW** — purpose, structure, and the commands that verify work.
- **No code-style rules in docs**; linters own that. **No stale snippets** — prefer `file:line`
  pointers over pasted code.
- **Hand-craft, don't `/init`.** Update the relevant doc when a rule changes; don't let tiers drift.

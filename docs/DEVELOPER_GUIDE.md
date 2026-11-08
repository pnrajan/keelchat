# Developer guide

A practical tour for developers working on yachat: get it running, walk one conversation through the API, find the
code you need, and debug what goes wrong. The deeper references are linked at each step.

## The model in one minute

- A **policy** (one YAML file) describes a bot: charter, flow, tools, guardrails, limits, memory. Building it
  validates every key and produces a frozen, hashed `CompiledPolicy`. Sessions pin that hash.
- The **registry** holds every capability (tools, flow nodes, guardrails, pre-processors). Capabilities are decorated
  Python, discovered at startup; a policy can only name what the registry has.
- The **runtime** takes each message through **Understand → Decide → Respond**. Counting (re-asks, digressions,
  planner budget) happens in code; the model never decides when a conversation ends.
- **Data services** run as separate processes and are reached through tools:
  the Knowledge Service (documents, `knowledge_search`) and the Analytics Service (tables, `analytics_query`).
- A **dataset** names where data lives (`structured` tables or `unstructured` documents). A flow step names its
  dataset in its config; the build checks it exists and has the right kind.

```
 browser ─► console :5173 ──/v1──► API :8000 ──► Knowledge Service :8001  (documents, vector index)
                                      │      └─► Analytics Service :8002  (DuckDB + Vanna, sandbox for Python)
                                      └─► Redis (sessions) · Postgres (memory, datasets)   — optional; in-memory if unset
```

Full component walkthrough: [`ARCHITECTURE.md`](ARCHITECTURE.md).

## Set up

Needs Python 3.12, [uv](https://docs.astral.sh/uv/), Node 20+, and a model provider key. Docker is optional: it
runs Redis/Postgres (`scripts/db.sh up`) and the local code sandbox.

```bash
cp .env.example .env              # set YACHAT_LLM and the matching *_API_KEY
uv sync --all-extras
./run.sh                          # console, API, Knowledge and Analytics Services
```

There is no fake model. Tests, simulations and evals call the provider in `.env`, so a key is needed even for
`pytest`.

## Run modes

| Command | What runs | Use it for |
|---|---|---|
| `./run.sh` | console (Vite, hot reload), API, Knowledge (indexes `examples/knowledge`), Analytics (registers `examples/data` as `sales`) | everyday development |
| `./run.sh --sandbox` | the same, plus statistics code in a local Docker sandbox (builds `yachat-sandbox:latest` on first use) | analytics questions that need Python |
| `./run.sh --build` | console built into `frontend/dist` and served by the API on :8000 | checking the production bundle |
| `./run.sh --fake-tools` | API and console only; every tool uses its in-memory fake | runtime work that does not touch the services |
| `scripts/try.sh` | built console and API, all in memory (ignores `DATABASE_URL` and `REDIS_URL`), with `support-assistant` saved and three tests; the next free port from 8000; `--judge` adds the judged checks | trying the Tests, Rate and Judge tabs |
| `scripts/dev.sh --knowledge` / `--analytics` | one data service; fakes for the other tools | working on one service |
| `scripts/knowledge.sh`, `scripts/analytics.sh` | one service on its own | service-only work, with `curl` |

Compiled policies live in the API's memory. **Saved versions** (**Save version** in the console, or
`POST /v1/tenants/{tenant}/agents`) are stored in Postgres when `DATABASE_URL` is set, and each agent's active version
is rebuilt from its YAML when the API starts. Sessions are in memory unless `REDIS_URL` is set. So without those,
**restarting the API (including the hot reload after any edit under `yachat/`) loses policies and sessions**.
The console says so: build the policy again and start a new session.

## One conversation, end to end

The console drives these same calls; here they are with `curl` so you can see each contract. With
`YACHAT_API_KEY` set, add `-H "Authorization: Bearer $YACHAT_API_KEY"` to each call. Interactive docs:
http://127.0.0.1:8000/docs.

**1. Register a dataset** (`./run.sh` already registers this one). Sources must sit under
`YACHAT_DATA_ROOT`; the Analytics Service loads them now, so a bad file fails here, not mid-conversation.
Registering an existing name replaces its sources, so list every table.

```bash
curl -s -X PUT localhost:8000/v1/tenants/default/datasets/sales -H 'content-type: application/json' -d '{
  "description": "Online store orders, January to June 2026",
  "spec": {"kind": "structured", "tables": [
    {"name": "orders", "uri": "'"$PWD"'/examples/data/orders.csv", "columns": {"amount": "order total in USD"}},
    {"name": "targets", "uri": "'"$PWD"'/examples/data/targets.parquet", "description": "Quarterly revenue target per region"}]}}'
# → {"tenant": "default", "name": "sales", "version": "<content hash>", ...}
```

**2. Build the policy.** Errors come back as a list of YAML paths, the same as `uv run yachat build`.

```bash
curl -s localhost:8000/v1/agents -H 'content-type: application/json' \
  -d "$(jq -n --rawfile y examples/sales_analyst.yaml '{yaml: $y}')"
# → {"agent": "sales-analyst", "hash": "0d5c…", "mode": "flow", "allowed_tools": ["analytics_query"], "datasets": ["sales"]}
```

**3. Open a session.** It pins the newest build of that agent (or `policy_hash`, if you pass one).

```bash
SID=$(curl -s localhost:8000/v1/sessions -H 'content-type: application/json' \
  -d '{"agent": "sales-analyst", "user_id": "dev"}' | jq -r .id)
```

**4. Send turns.** Each reply carries the outbound message, the per-stage trace, counts and any errors.

```bash
curl -s localhost:8000/v1/sessions/$SID/turns -H 'content-type: application/json' \
  -d '{"text": "Which region brought in the most revenue?"}' | jq '{text: .outbound.text, route, llm_calls, tool_calls, errors}'
```

An analytics answer also carries the full `AnalysisResult` as an item:
`jq '.outbound.items[] | select(.type == "analysis_result") | .result'`. Its fields (input, data, assumptions,
findings, summary, recommendations) are described in [`ARCHITECTURE.md`](ARCHITECTURE.md#datasets-and-the-analytics-service).

**5. Inspect and clean up.**

```bash
curl -s localhost:8000/v1/sessions/$SID/state | jq '{steps, fields, outputs, digressions, reasks}'
curl -s -X DELETE localhost:8000/v1/users/dev/memory      # forget everything remembered about this user
```

`uv run yachat simulate <policy.yaml> <script.yaml>` replays a scripted conversation the same way, without HTTP
(`examples/sim_script.yaml`).

## Where things live

| I want to… | Start here |
|---|---|
| change what a bot does | its YAML; keys and checklist in [`WRITING_AN_AGENT.md`](WRITING_AN_AGENT.md) |
| add a tool, flow node, guardrail or pre-processor | [`ADDING_A_CAPABILITY.md`](ADDING_A_CAPABILITY.md); code in `yachat/capabilities/` |
| change how messages are classified or routed | `yachat/runtime/decide/` (`intent_detector.py`, `orchestrator.py`, `planner.py`) |
| change tool timeouts, retries or scrubbing | `yachat/runtime/decide/tool_gateway.py` |
| change policy validation or its error messages | `yachat/policy/validator.py`, `compiler.py` |
| change what the policy editor completes and checks | `yachat/policy/schema.py` (JSON Schema from the models and registry), `frontend/src/policySchema.ts`, `PolicyEditor.tsx` |
| change how policies are saved and versioned | `AgentVersionStore` in `yachat/state/store.py` (+ `postgres_store.py`), `Runtime.save_version` |
| change how a policy is drafted from a description | `yachat/policy/drafter.py` |
| change the bot types or how agents are copied | `yachat/policy/templates.py` (built-ins come from `examples/`), `TemplateStore` in `state/store.py` |
| change the readiness checklist | `yachat/runtime/readiness.py` (checks), `frontend/src/Setup.tsx` (checklist and settings form) |
| change the Claude tools or plugin | `yachat/api/mcp_server.py` (`/mcp`), `plugins/yachat/` (skill, MCP config), `.claude-plugin/marketplace.json` |
| change how replies are phrased and checked | `yachat/runtime/respond/rewriter.py` (goal → words, fact check, per-step verifier) |
| change how analytics answers are written or checked | `yachat/analytics/analyst.py` (prompts and steps), `grounding.py` (number checks) |
| change how reasoning is planned and verified | `yachat/reasoning/` (plan, checks, verifier, rounds); used by analytics and knowledge |
| change where model-written code runs | `yachat/analytics/sandbox.py` (`YACHAT_SANDBOX`) |
| change document chunking or search | `yachat/knowledge/` |
| add a channel (Slack, SMS, …) | `yachat/channels/` |
| change the console | `frontend/src/` (`App.tsx`, `Analysis.tsx`, `styles.css`) |
| change how services are wired together | `yachat/runtime/runtime.py`, `service_from_env` in each service |

## Debugging

**Start with the console's Inspector.** *Trace* lists every step of the last turn with its time and notes (skipped
steps, the routing reason). *State* is the session as stored: step progress, collected fields, step outputs, counters. *Rendered*
shows the channel-specific payload. *Analysis* shows the full analysis behind an analytics reply, including the SQL
and any Python, and the verification of analytics and knowledge answers: the plan, each check with pass or fail, and
the problems the verifier found.

**Then the logs.** Every service logs JSON to stdout; each line carries `session_id`, `turn_id`, `policy_hash` and
`stage`, so one conversation can be followed with `grep <session_id>`. Useful messages: `routing` (why a step was
picked), `tool failed` (with the error kind), `analysis done` (status, attempts per stage). Sensitive values are scrubbed
before they reach a log line or a tool.

| Symptom | Likely cause | Fix |
|---|---|---|
| "… is unavailable right now" in a reply | the tool's backend URL is unset and fakes are off | start the service, or run with `--fake-tools` |
| Build fails with `unknown dataset` | the dataset is not registered for that tenant | register it first (step 1); the error suggests close names |
| Registration returns 422 | the source is outside `YACHAT_DATA_ROOT`, missing, empty, or unreadable | the message names the reason |
| API exits at start with a database error | `DATABASE_URL` names a database that does not exist | `scripts/db.sh up` and its URL, or leave it empty |
| "This session is no longer on the server" | the API restarted and sessions were in memory | build again and start a new session |
| "The API no longer has this policy" | the API restarted; compiled policies are kept in memory | press **Build & try** again, or save versions with `DATABASE_URL` set |
| Log line "saved version no longer builds" at start | a saved version names a capability or dataset that is gone | fix the YAML, save a new version and activate it |
| **Save version** is disabled | the editor's live check found problems: only valid builds are saved | fix the marked lines |
| Analysis `partial`, figures dropped | the model cited numbers the data does not contain (each must match a value, or a difference, ratio or percentage change of two, to its shown rounding), or a provider rate limit | read *Limitations*; retry when the limit clears |
| Analysis `partial` with "did not pass verification" | the verifier found problems it could not get fixed within `repairs.verify` rounds | read the problems under *Limitations* and the *Verification* section in the Analysis tab |
| "I couldn't confirm an answer to that from our documents." | the knowledge answer never passed verification against the passages | check the passages the search returned; the Analysis tab shows the verifier's problems |
| Statistics questions answered with SQL only | `YACHAT_SANDBOX` is unset | `./run.sh --sandbox`, or `YACHAT_SANDBOX=agentcore` |
| Bubblewrap sandbox fails inside Docker | the container blocks user namespaces | run it with `--security-opt systempaths=unconfined --security-opt seccomp=unconfined` |
| Console shows "API offline" | the API process is down | check the terminal running `./run.sh` |

## Checks before a pull request

The commands are in [`CONTRIBUTING.md`](../CONTRIBUTING.md#before-opening-a-pull-request). Use `uv run pytest -k <name>`
for one area (e.g. `-k analytics`), and `--offline` to run only the tests that don't call the model.

Tests run in parallel against your provider, which may rate-limit them; run the live console and the test suite at
different times. Capability changes must also pass `tests/test_registry.py` (the contract suite). PR rules:
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

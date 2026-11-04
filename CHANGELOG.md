# Changelog

All notable changes are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `from yachat import tool, node, ...`: the extension API in one import; `py.typed` so installed yachat exposes its types.
- `yachat new agent|package <name>`: starter files that build and pass the contract suite.
- `pytest --offline`: runs the tests that don't need a model when no key is set; CI runs them on fork PRs.
- `@metered` (`runtime/usage.py`): decorate an LLM client's provider call to record tokens, time and cost.
- Cost behind a LiteLLM gateway is that of the routed model (or the gateway's own cost), not unknown; Headroom's saved
  tokens are recorded and shown on the cost page.
- The Analytics Service returns its model calls with each answer; they count toward the turn that asked.
- Cost dashboard (`/usage`, `/usage/alternatives`) adds up in DuckDB over the whole window: Postgres by `COPY`, the Iceberg
  archive read by DuckDB straight from S3. It no longer stops at the newest 50,000 records.

### Fixed
- `./run.sh` no longer uninstalls extras installed with `uv sync --all-extras`.
- The booking example guardrail's `check()` is typed with `GuardContext`.

## [0.1.0]

### Added
- First open-source release of yachat: chatbots defined by one YAML policy and run on one shared runtime.
- The policy format with validation by YAML path, compilation, JSON Schema for editors, and a drafter that writes a
  policy from a plain description.
- The capability registry: tools, actions, flow steps, guardrails and pre-processors as decorated functions or
  classes, discovered from this package, from `YACHAT_CAPABILITY_PACKAGES` or from entry points; MCP servers and
  per-tenant HTTP tools; one contract suite for all of them.
- The runtime: Understand → Decide → Respond, intent detection, flow, adaptive and planner modes, the Tool Gateway,
  reply phrasing with a verifier, input and output guardrails, typed memory with consent rules, handoff to a person,
  an audit log of every decision, and token usage and cost per model.
- Built-in steps: consent, form fill, survey, conversational survey, knowledge answers, data analysis, job search,
  handoff and exit.
- Channels: website widget, link and installable app, WhatsApp Cloud API, phone calls through Twilio, browser voice,
  SMS and Slack adapters.
- Stores behind ports with in-memory versions: Redis, Postgres, S3 artifacts, mem0, an Iceberg audit archive.
- The Knowledge Service (semantic chunking, LanceDB) and the Analytics Service (DuckDB, SQL, sandboxed code).
- Simulated users, survey self-tests and an evals harness; a React test console.
- Examples, including an extension package with a custom tool, guardrail and channel
  (`examples/extensions/booking_caps`).

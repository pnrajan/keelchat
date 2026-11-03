# Changelog

All notable changes are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

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

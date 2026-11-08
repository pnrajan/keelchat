<div align="center">

# yachat

**Chatbots you write in YAML and extend in Python.**

[![CI](https://github.com/pnrajan/yachat/actions/workflows/ci.yml/badge.svg)](https://github.com/pnrajan/yachat/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange)](CHANGELOG.md)

**[Try the demo](https://yachat-demo.fly.dev)**: chat with the support assistant, no sign-up.

<img src="docs/assets/console.jpg" alt="The yachat test console: a policy on the left, a test chat in the middle, and why each reply was chosen on the right" width="900">

</div>

yachat runs chatbots that have a job to finish: support triage, bookings, intake forms, surveys. You describe each bot
in one short YAML file. One runtime runs them all, and enforces limits, guardrails and tool access in code, not in a
prompt.

- **One file per bot.** What it is for, what it asks, which tools it may use, when it hands over to a person.
- **Code decides, the model phrases.** Re-asks, digressions and exits are counted by the runtime.
- **Extend in Python.** A tool, step, guardrail or channel is one decorated function or class, or an MCP server.
- **Any model, any channel.** Anthropic or anything litellm reaches; web, WhatsApp, SMS, Slack and phone.
- **Tested from the policy.** Test sets are generated from the YAML, and a stronger model reviews trial chats.

## Quickstart

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 20+ and one model key.

```bash
git clone https://github.com/pnrajan/yachat && cd yachat
cp .env.example .env        # set YACHAT_LLM and its *_API_KEY
uv sync --all-extras
scripts/try.sh              # console with a sample bot, everything in memory
```

In the console, pick **Support assistant**, press **Build & try**, then **New session**, and chat. Or open **Compose**
and describe the bot you want in plain words.

## Your first bot

```yaml
agent: support-assistant
charter: >
  Help customers with orders and returns. Friendly, concise.
  Out of scope: legal or medical advice.
languages: [en, es]
tools: [knowledge_search]
flow:
  - greet_consent
  - form_fill:
      fields:
        - {name: order_number, ask: "What is your order number?", expect: "An order number in any format, such as ORD-12345 or #98765"}
        - {name: issue, ask: "What went wrong?", expect: "A short description", optional: true}
guardrails: {input: [prompt_injection], output: [pii, no_legal_advice]}
limits: {reasks: 3, digressions: 3}
handoff: {when: [user_request, max_reasks]}
```

It asks for consent, collects an order number, answers side questions from your documents, blocks prompt
injection, removes personal details from replies, and hands over to a person when asked or stuck.

```bash
uv run yachat build my_bot.yaml   # validate and compile
uv run yachat serve               # HTTP API; interactive docs at /docs
```

[The policy language](docs/POLICY_LANGUAGE.md) lists every key. [Examples](examples) has bots for bookings, surveys,
hiring and data questions.

## Add a tool

```python
from yachat import tool


@tool("slot_search", timeout_s=3.0, retries=1)
async def slot_search(service: str, day: str = "") -> dict[str, list[str]]:
    """Open appointment slots for a service, optionally on one day."""
    return {"slots": await my_booking_api.free_slots(service, day)}
```

Type hints become the schema; the docstring is what the model reads. Allow it with `tools: [slot_search]`.
`uv run yachat new package my_caps` writes a starter package with a tool, a step, a guardrail and a passing test.

## Test it

```bash
uv run yachat evals generate my_bot.yaml          # a test set from the policy
uv run yachat evals check evals/my_bot            # checks every conversation must pass
```

In the console, **Test → Judge** has a stronger model (`YACHAT_MODEL_CRITIC`) review a chat and propose fixes.

## Docs

| To… | Read |
|---|---|
| write and tune a bot | [Writing an agent](docs/WRITING_AN_AGENT.md) |
| look up a policy key | [The policy language](docs/POLICY_LANGUAGE.md) |
| use the console, channels, memory, handoff, documents | [Features](docs/FEATURES.md) |
| add a tool, step, guardrail or channel | [Adding a capability](docs/ADDING_A_CAPABILITY.md) |
| test and measure quality | [Evals](docs/EVALS.md) |
| look up the CLI, API, settings, deployment | [Reference](docs/REFERENCE.md) |
| compare with Rasa, Parlant, LangGraph, Botpress | [Comparison](docs/COMPARISON.md) |

## Help and contributing

Questions go to [Discussions](https://github.com/pnrajan/yachat/discussions), bugs to
[issues](https://github.com/pnrajan/yachat/issues/new/choose), vulnerabilities to [SECURITY.md](SECURITY.md).
Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md).

[Apache-2.0](LICENSE)

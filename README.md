<div align="center">

# yachat

**Customer-facing chatbots that follow your rules.**<br>
Describe a bot in plain English or write it in YAML. The runtime, not the model, enforces its steps, limits and
handover to a person.

[![CI](https://github.com/pnrajan/yachat/actions/workflows/ci.yml/badge.svg)](https://github.com/pnrajan/yachat/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab?logo=python&logoColor=white)](pyproject.toml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)
[![Status: alpha](https://img.shields.io/badge/status-alpha-orange)](#status)

**[Try it in your browser](https://yachat-demo.fly.dev)** · [Run it locally](#run-it-locally) ·
[Deploy to Render](#deploy-to-render) · [Docs](#docs) · [Website](https://pnrajan.github.io/yachat/)

<img src="docs/assets/make-a-bot.gif" alt="In the yachat console: a dental clinic's booking bot is described in two sentences, yachat writes it and reads it back in plain words, then a test chat where a patient mentions a knocked-out tooth and the bot hands the chat to a person" width="900">

</div>

## Why yachat

A chatbot that books appointments or takes support requests has a job to finish. Prompt-only bots drift: they skip
questions, loop, or promise a person and never hand over. yachat keeps the model for reading and phrasing, and puts
every decision in code you can review.

- **Plain English in, a reviewable file out.** Describe the bot, read back what it will do, change it in plain words.
  The result is one short YAML file.
- **Code decides, the model phrases.** Answers are validated, re-asks and budgets are counted, and handover happens
  in code. A model never decides to end a conversation.
- **Safe by default.** Tools are called through one gateway (allow list, timeouts, circuit breaker), with input and
  output guardrails, and memory only for listed items, with consent.
- **Any model, any channel.** Anthropic, or anything litellm reaches (OpenAI, Gemini, Bedrock, OpenRouter, Ollama).
  Web widget, WhatsApp, SMS, Slack and phone.
- **Tested from the policy.** Test sets are generated from the YAML; a stronger model reviews trial chats and
  proposes fixes.
- **Yours to run and extend.** Apache-2.0 and one Docker image. A tool, step, guardrail or channel is one decorated
  Python function or class, or an MCP server.

**A good fit:** support triage, bookings, intake forms, surveys; many bots on one self-hosted runtime.
**Not a fit:** a free-form assistant, multi-agent research, or anything that needs stable APIs today
([comparison](#how-yachat-compares)).

## Get started

| | Time | You need | |
|---|---|---|---|
| **Try it in your browser** | 1 min | nothing | [yachat-demo.fly.dev](https://yachat-demo.fly.dev) |
| **Run it locally** | 5 min | macOS, Linux or WSL 2, a model key | [steps](#run-it-locally) |
| **Deploy to Render** | 10 min | GitHub and Render accounts, a model key | [steps](#deploy-to-render) |

### Try it in your browser

In the [demo](https://yachat-demo.fly.dev), with no install or sign-in, describe a bot and get its YAML. Press
**Try this bot** to chat with it (its tools answer with sample data), and see what it needs connecting before real
people use it. A link can start a draft:
[`?describe=Book haircuts and ask for a phone number`](https://yachat-demo.fly.dev/?describe=Book+haircuts+and+ask+for+a+phone+number).

<p align="center"><img src="docs/assets/demo-make.jpg" alt="The demo: a dental booking bot described in one sentence, its YAML with Copy and Download, and what it needs connecting: a booking calendar (Open Dental, Dentrix, Calendly), a model key, somewhere for handed-over chats, and where people talk to it" width="560"></p>

### Run it locally

```bash
git clone https://github.com/pnrajan/yachat && cd yachat
./setup.sh        # checks for git, uv, Node 20+; offers to install what's missing; asks for your model key
scripts/try.sh    # opens the console with a sample bot, everything in memory
```

In the console, open **Compose**, describe a bot, press **Create my bot**, then **Try it**. The **Inspector** shows
why each reply was chosen. For the whole stack in Docker (API, console, Redis, Postgres, data kept between restarts):
`scripts/local.sh up`. From Claude Code, the [yachat plugin](plugins/yachat) builds and tries bots when you ask in
plain words.

### Deploy to Render

Run yachat in your own Render account on the free plan, rebuilt each time CI passes on your fork.

1. **Fork** this repository, so the deploy follows your copy.
2. Open `https://render.com/deploy?repo=https://github.com/<you>/yachat`. (The button below deploys this
   repository as is, without your changes.)

   [![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/pnrajan/yachat)

3. Sign in to Render, enter your `ANTHROPIC_API_KEY` when asked, and confirm. The first build
   takes a few minutes.
4. On the service's **Environment** page, copy `YACHAT_API_KEY`. Render generated it; it locks the console and API.
5. Open `https://<service>.onrender.com`. The console asks for the key once.

Good to know:

- **Another model provider:** on the **Environment** page, set `YACHAT_LLM=litellm`, `YACHAT_MODEL_GENERATE`,
  `YACHAT_MODEL_CLASSIFY` and the provider's key (e.g. `OPENROUTER_API_KEY`), as in [`.env.example`](.env.example).
- **Saved bots need a database.** Without `DATABASE_URL` (and `REDIS_URL`), everything is in memory and is lost when
  the service restarts or sleeps. Add a Postgres URL on the **Environment** page.
- **The free plan sleeps** when idle, so the first request after that is slow. A paid plan stays awake.
- Settings are in [`render.yaml`](render.yaml). For **Fly.io** and other hosts, see
  [Deploy from GitHub](docs/REFERENCE.md#deploy-from-github).

## Your first bot

Save this as `my_bot.yaml`:

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
uv run yachat build my_bot.yaml   # validate and compile; errors name the YAML path
```

Paste it into the console's editor and press **Build & try**, or send it to the API with `POST /v1/agents`
([walkthrough](docs/DEVELOPER_GUIDE.md#one-conversation-end-to-end)). [The policy language](docs/POLICY_LANGUAGE.md)
lists every key; [examples](examples) has bots for bookings, surveys, hiring and data questions.

## How it works

Every message goes through three stages. The model reads and phrases; the decisions in between are code.

```mermaid
flowchart LR
  U[User message] --> A["Understand<br>language, intent, input guardrails"]
  A --> B["Decide<br>the policy's current step, rules,<br>counters, tool gateway"]
  B --> C["Respond<br>phrase the reply, check it,<br>output guardrails, channel limits"]
  C --> R[Reply]
```

Each conversation is pinned to the compiled policy it started on, so a change never alters a chat in progress.
[Architecture](docs/ARCHITECTURE.md) has the detail.

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
uv run yachat evals generate my_bot.yaml           # a test set from the policy, in evals/<agent>/
uv run yachat evals check evals/support_assistant  # the checks every conversation must pass
```

In the console, **Test → Judge** has a stronger model review a chat and propose fixes to the policy or the code.

## Where a stronger model helps

yachat runs well on a small, cheap model for everyday turns. A stronger model pays off in a few places, each one
setting:

| Job | Why a stronger model helps | Setting |
|---|---|---|
| Writing a bot from English | Cleaner YAML on the first try, fewer build repairs | `YACHAT_MODEL_GENERATE` |
| Reviewing trial chats | Spots what went wrong on which turn and proposes fixes | `YACHAT_MODEL_CRITIC` |
| Hard or risky steps | `high_stakes: true` steps use the large model and are always checked | the policy |
| Checking plans before replying | `think: true` steps plan first; a separate model can check the plan | `YACHAT_MODEL_VERIFY` |
| Judging test results | Scores scope and rule-following in evals | `EVALS_JUDGE_MODEL` |

Classification and routine replies stay on `YACHAT_MODEL_CLASSIFY`. [Model routing and cost](docs/REFERENCE.md) has
the detail.

## How yachat compares

| | **yachat** | **Parlant** | **Rasa** | **LangGraph** | **Botpress** |
|---|---|---|---|---|---|
| You write | One YAML file per bot, or plain English | Python guidelines and journeys | YAML flows and Python actions | Python graph code | Visual flows |
| Who decides the next step | Code, from the policy | The model, steered by matched guidelines | Code flows | Your code | Flows plus model nodes |
| Limits, guardrails, handoff | Built in, counted in code | Built in | Built in | You build them | Platform features |
| Open source | Apache-2.0 | Apache-2.0 | Pro is commercial; open source is in maintenance | MIT | Hosted; SDK is MIT |
| Maturity | Alpha | Active | Mature | Mature | Mature |

[How yachat compares](docs/COMPARISON.md) has the detail and sources.

## Docs

| To… | Read |
|---|---|
| write and tune a bot | [Writing an agent](docs/WRITING_AN_AGENT.md) |
| look up a policy key | [The policy language](docs/POLICY_LANGUAGE.md) |
| use the console, channels, memory, handoff, documents, Claude Code | [Features](docs/FEATURES.md) |
| add a tool, step, guardrail or channel | [Adding a capability](docs/ADDING_A_CAPABILITY.md) |
| test and measure quality | [Evals](docs/EVALS.md) |
| look up the CLI, API, settings, deployment | [Reference](docs/REFERENCE.md) |
| understand the runtime | [Architecture](docs/ARCHITECTURE.md) |

## Help and contributing

Questions and bots to show go to [Discussions](https://github.com/pnrajan/yachat/discussions), bugs to
[issues](https://github.com/pnrajan/yachat/issues/new/choose), vulnerabilities to [SECURITY.md](SECURITY.md).
Contributions are welcome: start with a
[good first issue](https://github.com/pnrajan/yachat/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22), and
see [CONTRIBUTING.md](CONTRIBUTING.md) and the [code of conduct](CODE_OF_CONDUCT.md).
If yachat is useful to you, a star helps others find it.

## Status

Alpha (0.x): the policy language and APIs may still change between minor versions; [CHANGELOG.md](CHANGELOG.md)
lists every change.

## License

[Apache-2.0](LICENSE)

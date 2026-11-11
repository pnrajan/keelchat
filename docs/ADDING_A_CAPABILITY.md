# Adding a capability

One decorated function or class. No runtime change. Everything an extension needs imports from the top level:
`from keelchat import tool, node, Node, NodeResult, guardrail, ...`.

## Start here: your first step in ten minutes

```bash
uv run keelchat new package acme_caps --out my_bot      # a tool, a step, a guardrail, an agent and a contract test
cd my_bot
PYTHONPATH=. KEELCHAT_CAPABILITY_PACKAGES=acme_caps uv run --project <keelchat checkout> keelchat build acme-caps-agent.yaml
PYTHONPATH=. uv run --project <keelchat checkout> pytest test_acme_caps.py
```

The files build and pass the contract suite as written. Read them in this order:

1. **`acme_caps/tools.py`: the tool.** An async function. Its type hints become the schema, and its docstring is what
   the model reads. It talks to your system; here a dictionary stands in.
2. **`acme_caps/steps.py`: the step.** The core idea of keelchat is that **code decides and the model phrases**. Each
   turn the runtime calls `act()`, and you return one `NodeResult`:
   - **Need something from the user?** Return `ask=(field, sample wording, expect)`. The model words the question
     to fit the conversation. The answer is checked against `expect` before it reaches you.
   - **Got the answer?** Next turn, `answers_for(ctx, "<step name>")` holds the checked value.
   - **Call the tool** through `ctx.services.gateway.call(...)`, never directly. The gateway applies the allow list,
     timeout, retries and scrubbing.
   - **Done?** Return `outputs={...}, complete=True`. The flow moves to the next step in the same turn.
   - `text` is the plain reply with the facts, and `goal` is what the reply must achieve. The model rewrites `text`
     to meet `goal`, and a checker makes sure nothing is invented.
3. **`acme_caps/guardrails.py`: the guardrail.** It runs on every reply, flags what it finds and supplies a
   replacement.
4. **`acme-caps-agent.yaml`: the agent.** It uses the step by name in `flow:` and the guardrail by name in
   `guardrails:`.

Then change the tool to call your system, and run the build and the test again. The sections below cover every option.

## Tool

```python
from keelchat import tool


@tool("salary_lookup", timeout_s=3.0, retries=1)  # translate=False, side_effects="write" as needed
async def salary_lookup(title: str, location: str = "") -> dict[str, str]:
    """Median salary for a title in a location."""  # the docstring is the description shown to the planner
    ...
```

Input schema comes from the type hints, output schema from the return hint. Raise `ToolError(name, kind, message)`
for failures. `retries` is how many times the gateway retries after a timeout or error (default 1); after that a
circuit breaker opens for the tool. Never open sockets outside the function the gateway calls. Tool output goes into
prompts as data, never as instructions. Anything that changes something (book, pay, submit) is an
[action](#candidate-only-action), not a tool. Allow it in a policy with `tools: [salary_lookup]`. Full example:
[`examples/extensions/booking_caps/tools.py`](../examples/extensions/booking_caps/tools.py).

## MCP server

No code. List the server in `KEELCHAT_MCP_SERVERS`; every tool it exposes is registered as `<alias>.<tool>` with
the server's schema and description, and runs through the Tool Gateway like any other tool:

```bash
KEELCHAT_MCP_SERVERS="tm=https://talent.example.com/mcp, time=npx -y @modelcontextprotocol/server-time"
```

A target starting with `http` is a streamable-HTTP endpoint; anything else is a command started over stdio. Servers
are contacted once at startup, each bounded by a timeout; one that is down or misconfigured is logged and skipped.
A tool the server marks destructive is a write: never retried and never offered to the planner. Each call opens its
own session, so a stdio server starts its command per call; prefer HTTP servers in production. Reference the tools
in a policy as `tools: [tm.career_navigation]`.

## HTTP endpoint (Tool Registry API)

A customer registers an endpoint at runtime; it becomes a tool visible only to that tenant's agents:

```bash
curl -X POST localhost:8000/v1/tenants/acme/tools -H 'content-type: application/json' -d '{
  "name": "crm_lookup",
  "description": "Customer tier and open tickets from the CRM.",
  "url": "https://crm.acme.example/lookup",
  "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}}, "required": ["customer_id"]},
  "output_schema": {"type": "object"},
  "timeout_s": 5, "retries": 1, "side_effects": "none",
  "headers": {"authorization": "Bearer ..."}
}'
```

The gateway POSTs the validated arguments as JSON and treats the JSON reply as data. The URL must resolve to a
public address (`KEELCHAT_ALLOW_PRIVATE_TOOL_URLS=1` lifts this for local development). Mark endpoints that
change something `"side_effects": "write"` with `"retries": 0`: they are never retried or offered to the planner.
A name is immutable once registered, because pinned policies may use it; register a new name to change one. The
registration passes the contract suite before it goes live; `GET /v1/tenants/{tenant}/tools` lists a tenant's tools (never their headers),
`GET /v1/registry?tenant=acme` shows platform, MCP and tenant tools together. With `DATABASE_URL` set,
registrations are stored in Postgres (`tenant_tools`): they survive restarts, every replica loads them, and the
table's key settles duplicate names. Without it they live in process memory. Registered headers are stored as given,
so treat that table like any other secret store.

## Node

```python
from keelchat import CapabilityConfig, Node, NodeResult, node


class MyConfig(CapabilityConfig):
    greeting: str = "Hi"


@node("my_step", tools=["knowledge_search"], produces=["my_output"])
class MyStep(Node[MyConfig]):
    config_model = MyConfig

    async def act(self, ctx, config):
        return NodeResult("…", ask=("field", "Ask…", "expect…"))  # or outputs={...}, complete=True
```

`produces` are the outputs that make the step complete; `tools` join the agent's allow list; `requires:` on a flow
entry orders steps by those outputs. Use it in a policy as `- my_step: {greeting: Hello}`. A worked example that
calls a tool, offers choices and completes on the user's pick:
[`examples/extensions/booking_caps/steps.py`](../examples/extensions/booking_caps/steps.py). The built-in steps are
`greet_consent`, `form_fill`, `survey`, `conversation_survey`, `knowledge`, `analytics`, `job_search`, `handoff` and
`exit`; [`form_fill.py`](../keelchat/capabilities/nodes/form_fill.py) is a good one to read first.

- **Datasets.** A config field typed `StructuredRef` or `UnstructuredRef` (`keelchat.datasets.models`) names a
  dataset; the build checks it exists with that kind. A tool that reads one declares `@tool(..., dataset="structured")`
  and takes `dataset: Dataset`: the model passes a name, the gateway passes the tenant's resolved dataset.
- **Repairs.** `@node(..., repairs={"sql": 3, "code": 3})` sets how many times each stage may fix its own model
  output from the error it got. Each value is the default and the maximum; policies may lower it (`limits.repairs`,
  or `repairs:` on the step). Without it a node gets one `default` stage at the platform value (2). A tool declares
  the same with `repairs=` and a `repairs: dict[str, int] | None = None` parameter, which the gateway fills and caps.
- **Verified reasoning.** A capability that reasons or calculates runs its work through
  `keelchat.reasoning.verified(llm, models, task, rounds)`. The task supplies `brief()` (the question and trusted
  context), `produce(plan, checks, feedback)` (one attempt, with the reviewer's problems from the last one),
  `evidence()` and `answer()` (what the verifier reads), `results()` (checks it ran itself, e.g. assertions in a
  sandbox), and `stages` (where a problem can be sent back). The loop writes the plan and checks before the first
  attempt, verifies each attempt, and counts rounds; declare the budget as a `verify` repair stage
  (`repairs={"verify": 2}`) and pass the step's value as `rounds`. When `passed` is false, do not present the result
  as verified. Examples: `capabilities/nodes/knowledge.py` (claims checked against passages) and
  `analytics/analyst.py` (code checks enforced by `final_answer`).
- **Replies are directed, not scripted.** A node does not write the conversation. Its `NodeResult` carries `goal`
  (what the reply must achieve this turn, in a sentence) and `text` (a plain version with the facts; sent as is when
  replies are not rewritten or the model is down). When it needs something from the user it sets `ask=(field, sample
  wording, expect)`: the model phrases the question from the conversation, and the answer is still validated in code
  against `expect`. The node's docstring is its ability, shown to the model. See `capabilities/nodes/form_fill.py`.
- **Reply verifier.** `@node(..., verify=True)` (the default) checks each phrased reply before it is sent: the goal is
  met, the right thing is asked, nothing is invented. Problems get one rewrite, then the plain version is sent.
  `verify=False` passes replies through; a policy can override per step (`- greet_consent: {verify: false}`).
- **Questions as input.** `takes_questions = True` on the class routes in-scope questions to the step instead of
  counting them as digressions (an analyst, a Q&A step).

## Candidate-only action

```python
from keelchat import action


@action("withdraw_application", by="candidate")
async def withdraw_application(application_id: str) -> dict[str, str]: ...
```

Not a tool: the gateway rejects it from nodes and the planner. It runs only through `ActionRunner` when the
candidate's own action proves it (`InboundMessage.action` from the channel, or an explicit reply a node maps to it).

## Pre-processor

```python
from keelchat import PreProcessor, preprocessor


# built-ins: receiver 10, opt_out 15, handoff_request 16, language 20, shorthand 30, entity_normalization 40,
# input_guardrails 50, confirm_stop 55
@preprocessor("spam_filter", order=45)
class SpamFilter(PreProcessor):
    async def run(self, ctx):
        if looks_like_spam(ctx.text):
            ctx.short_circuit_with("I can't process that message.")
        return ctx
```

## Guardrail

```python
from keelchat import Guardrail, GuardResult, guardrail


@guardrail("no_competitors", stage="output")
class NoCompetitors(Guardrail):
    async def check(self, text, ctx):
        return GuardResult(flagged=True, reason="…", redacted=text.replace("Globex", "another company"))
```

List it in a policy: `guardrails: {output: [pii, no_competitors]}`. `prompt_injection` and `pii` always run. Input
guardrails can stop a turn; output guardrails redact or replace the reply. Flagged turns are never written to memory.
Full example: [`examples/extensions/booking_caps/guardrails.py`](../examples/extensions/booking_caps/guardrails.py).

## Channel

A channel turns its platform's payload into an `InboundMessage`, and a channel-neutral reply into its own messages
within its limits.

```python
from keelchat.channels import CHANNELS
from keelchat.channels.base import Channel, split_text
from keelchat.runtime.context import ChannelMessage, InboundMessage


class Telegram(Channel):
    name = "telegram"

    def normalize(self, payload):
        m = payload["message"]
        return InboundMessage(text=m.get("text", ""), channel="telegram", user_id=f"tg:{m['chat']['id']}", channel_message_id=str(m["message_id"]))

    def render(self, message):
        return [ChannelMessage(chunk) for chunk in split_text(message.text, 4096)]


CHANNELS.setdefault("telegram", Telegram())
```

Open sessions with `"channel": "telegram"` and post the raw update as the turn's `payload`; your webhook sends the
rendered messages back. Override `compliance()` for opt-out words the platform requires (see `channels/sms.py`). Full
example: [`examples/extensions/booking_caps/telegram.py`](../examples/extensions/booking_caps/telegram.py).

## Knowledge search parts

Knowledge search has four replaceable parts. Each has an interface, a decorator that registers it by name, and a
setting that picks it (a registered name, or `package.module:Name` without registering). All are importable from
`keelchat`.

| Part | Interface | Register | Setting | Built in |
|---|---|---|---|---|
| Embedding model | `Embedder`: `dim`, `id`, `embed(texts)` | `@embedder("name")` | `KNOWLEDGE_EMBEDDER` (+ `KNOWLEDGE_EMBED_MODEL`) | `litellm`, `sentence-transformers`, `siliconflow`, `hash` |
| Splitting into passages | `Chunker`: `chunk(text)` | `@chunker("name")` | `KNOWLEDGE_CHUNKER` | `semantic` (default), `sentences` |
| Indexing method | `Indexer`: `chunks(doc)` | `@indexer("name")` | `KNOWLEDGE_STRATEGY`, or `strategy:` on a knowledge base | `simple`, `contextual`, `raptor`, `full_context` |
| Vector store | `VectorIndex`: `upsert`, `search`, `delete_doc`, `count` | `@vector_store("name")` | `KNOWLEDGE_VECTOR_STORE` | `memory`, `lancedb` |

What each factory receives: an embedder its model name (`KNOWLEDGE_EMBED_MODEL`, or nothing); a chunker the
embedder; an indexing method an `IndexContext` (`embedder`, `chunker`, and `llm`/`models` for summaries, which
`need_llm()` returns or refuses); a vector store a `VectorStoreConfig` (`uri`, a `table` per embedder, `dim`).

```python
from keelchat import Embedder, VectorIndex, VectorStoreConfig, embedder, vector_store


@embedder("acme")
class AcmeEmbedder(Embedder):
    dim = 768

    def __init__(self, model: str = "acme-embed-1"):
        self.model = model

    @property
    def id(self) -> str:  # names the model: each embedder's vectors get their own table
        return f"acme:{self.model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return await acme_client.embed(self.model, texts)


@vector_store("pgvector")
class PgVectorIndex(VectorIndex):
    def __init__(self, cfg: VectorStoreConfig):
        self.table, self.dim = cfg.table, cfg.dim

    ...  # upsert, search (filter on chunk.dataset), delete_doc, count
```

Then `KNOWLEDGE_EMBEDDER=acme KNOWLEDGE_VECTOR_STORE=pgvector`, with the package made discoverable as below. A custom
indexing method is also offered in the console's **Knowledge base** panel. `GET /v1/registry` lists every part under
`knowledge`, and which are in use. Switching embedders re-indexes rather than mixing vectors that can't be compared.

## Make it discoverable

- In this repo: put the module under `keelchat/capabilities/`.
- In any importable package: list it in `KEELCHAT_CAPABILITY_PACKAGES=acme_caps,other_pkg`; every module in it
  is imported at startup and at build.
- In your own package: declare an entry point and ship it (`examples/contrib_tool` is the template):

```toml
[project.entry-points."keelchat.capabilities"]
salary_lookup = "contrib_tool.salary:register"
```

## Pass the contract suite

```python
from keelchat import check_capability

await check_capability("tool", "salary_lookup", fake_impl)
```

It checks: schemas are valid JSON Schema, a fake call succeeds, the gateway timeout is honored, no socket is opened
outside the Tool Gateway, actions are rejected without the candidate's action, and pre-processors return the context. `tests/test_registry.py` runs it over everything registered. Then reference the
name in a policy and run `keelchat build`.

# Adding a capability

One decorated function or class. No runtime change.

## Tool

```python
from yachat.registry.core import tool


@tool("salary_lookup", timeout_s=3.0, retries=1)  # translate=False, side_effects="write" as needed
async def salary_lookup(title: str, location: str = "") -> dict[str, str]:
    """Median salary for a title in a location."""  # the docstring is the description shown to the planner
    ...
```

Input schema comes from the type hints, output schema from the return hint. Raise `ToolError(name, kind, message)`
for failures. `retries` is how many times the gateway retries after a timeout or error (default 1). Never open
sockets outside the function the gateway calls.

## MCP server

No code. List the server in `YACHAT_MCP_SERVERS`; every tool it exposes is registered as `<alias>.<tool>` with
the server's schema and description, and runs through the Tool Gateway like any other tool:

```bash
YACHAT_MCP_SERVERS="tm=https://talent.example.com/mcp, time=npx -y @modelcontextprotocol/server-time"
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
public address (`YACHAT_ALLOW_PRIVATE_TOOL_URLS=1` lifts this for local development). Mark endpoints that
change something `"side_effects": "write"` with `"retries": 0`: they are never retried or offered to the planner.
A name is immutable once registered, because pinned policies may use it; register a new name to change one. The
registration passes the contract suite before it goes live; `GET /v1/tenants/{tenant}/tools` lists a tenant's tools (never their headers),
`GET /v1/registry?tenant=acme` shows platform, MCP and tenant tools together. With `DATABASE_URL` set,
registrations are stored in Postgres (`tenant_tools`): they survive restarts, every replica loads them, and the
table's key settles duplicate names. Without it they live in process memory. Registered headers are stored as given,
so treat that table like any other secret store.

## Node

```python
from yachat.registry.core import CapabilityConfig, Node, NodeResult, node


class MyConfig(CapabilityConfig):
    greeting: str = "Hi"


@node("my_step", tools=["knowledge_search"], produces=["my_output"])
class MyStep(Node[MyConfig]):
    config_model = MyConfig

    async def act(self, ctx, config):
        return NodeResult("…", ask=("field", "Ask…", "expect…"))  # or outputs={...}, complete=True
```

`produces` are the outputs that make the step complete; `tools` join the agent's allow list.

- **Datasets.** A config field typed `StructuredRef` or `UnstructuredRef` (`yachat.datasets.models`) names a
  dataset; the build checks it exists with that kind. A tool that reads one declares `@tool(..., dataset="structured")`
  and takes `dataset: Dataset`: the model passes a name, the gateway passes the tenant's resolved dataset.
- **Repairs.** `@node(..., repairs={"sql": 3, "code": 3})` sets how many times each stage may fix its own model
  output from the error it got. Each value is the default and the maximum; policies may lower it (`limits.repairs`,
  or `repairs:` on the step). Without it a node gets one `default` stage at the platform value (2). A tool declares
  the same with `repairs=` and a `repairs: dict[str, int] | None = None` parameter, which the gateway fills and caps.
- **Verified reasoning.** A capability that reasons or calculates runs its work through
  `yachat.reasoning.verified(llm, models, task, rounds)`. The task supplies `brief()` (the question and trusted
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
from yachat.registry.core import action


@action("withdraw_application", by="candidate")
async def withdraw_application(application_id: str) -> dict[str, str]: ...
```

Not a tool: the gateway rejects it from nodes and the planner. It runs only through `ActionRunner` when the
candidate's own action proves it (`InboundMessage.action` from the channel, or an explicit reply a node maps to it).

## Pre-processor

```python
from yachat.registry.core import PreProcessor, preprocessor


@preprocessor("spam_filter", order=45)  # built-ins: receiver 10, opt_out 15, language 20, shorthand 30, entity_normalization 40, input_guardrails 50
class SpamFilter(PreProcessor):
    async def run(self, ctx):
        if looks_like_spam(ctx.text):
            ctx.short_circuit_with("I can't process that message.")
        return ctx
```

## Guardrail

```python
from yachat.registry.core import Guardrail, GuardResult, guardrail


@guardrail("no_competitors", stage="output")
class NoCompetitors(Guardrail):
    async def check(self, text, ctx):
        return GuardResult(flagged=True, reason="…", redacted=text.replace("Globex", "another company"))
```

## Make it discoverable

- In this repo: put the module under `yachat/capabilities/`.
- In any importable package: list it in `YACHAT_CAPABILITY_PACKAGES=acme_caps,other_pkg`; every module in it
  is imported at startup and at build.
- In your own package: declare an entry point and ship it (`examples/contrib_tool` is the template):

```toml
[project.entry-points."yachat.capabilities"]
salary_lookup = "contrib_tool.salary:register"
```

## Pass the contract suite

```python
from yachat.registry.contract import check_capability

await check_capability("tool", "salary_lookup", fake_impl)
```

It checks: schemas are valid JSON Schema, a fake call succeeds, the gateway timeout is honored, no socket is opened
outside the Tool Gateway, actions are rejected without the candidate's action, and pre-processors return the context. `tests/test_registry.py` runs it over everything registered. Then reference the
name in a policy and run `yachat build`.

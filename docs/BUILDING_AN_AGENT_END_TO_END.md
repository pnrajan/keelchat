# Building a complex agent, end to end

This guide builds one real agent from an empty workspace to a launched, tested, versioned bot: **returns-desk**, a
support agent for an online shoe store. It asks for consent, collects an order number and the problem, looks the
order up in your own system, answers policy questions from your help-centre documents, speaks English and Spanish,
remembers how each customer likes to be contacted, and hands back a clear answer. Along the way it uses every part of
the builder: bot types, the editor, knowledge, a custom tool, safety checks, readiness, tests, versions and launch.

You can do each step in the console, over the API, or by asking Claude with the yachat plugin. All three call the
same API and the same checks. The console path is shown first; the API call follows.

| Step | What you end with |
|---|---|
| [1. Plan the job](#1-plan-the-job) | one paragraph that every later choice is checked against |
| [2. Set up a workspace](#2-set-up-a-workspace) | a running server and a key for your tenant |
| [3. Add what it knows](#3-add-what-it-knows) | help-centre documents in the Knowledge Service |
| [4. Connect your system](#4-connect-your-system) | an `order_lookup` tool the agent may call |
| [5. Create the agent](#5-create-the-agent) | v1 from the Support bot type |
| [6. Shape the policy](#6-shape-the-policy) | the full policy, in the Setup form or YAML |
| [7. Check readiness](#7-check-readiness) | nothing blocking launch |
| [8. Try it](#8-try-it) | conversations that read naturally and finish the job |
| [9. Write tests](#9-write-tests) | a replay set that gates every change |
| [10. Save, launch and roll back](#10-save-launch-and-roll-back) | a live version, and the way back |
| [11. Improve it](#11-improve-it) | real conversations turned into tests |
| [12. Do it all with Claude](#12-do-it-all-with-claude) | the same agent, built from a chat |

## 1. Plan the job

Write this down before touching the builder. It is the brief for the charter, the flow and the tests.

- **Goal:** a customer with a damaged or wrong order leaves with the problem recorded and knows what happens next.
- **Who:** shoppers on the website and WhatsApp, in English or Spanish.
- **Collect:** the order number (`ORD-` and five digits) and what went wrong.
- **Look up:** the order's status in our order system.
- **Know:** the returns policy, shipping times, refund times (from the help centre).
- **Never:** promise a refund amount, give legal advice, talk about other stores.
- **Remember:** how the customer prefers to be contacted, for next time.

Anything the agent must *do* becomes a step; anything it must *know* becomes knowledge; anything it must *never do*
goes in the charter and the safety checks.

## 2. Set up a workspace

```bash
cp .env.example .env                 # YACHAT_LLM and the provider key
scripts/db.sh up                     # Postgres + Redis in Docker: versions, bot types, memory and sessions persist
# in .env:
#   DATABASE_URL=postgresql://yachat:yachat@127.0.0.1:5432/yachat
#   YACHAT_API_KEY=<admin key>                 # every route needs a key
./run.sh                             # console :5173, API :8000, Knowledge :8001, Analytics :8002
# issue a key for tenant "acme" (shown once; stored only as a hash):
curl -s -H "Authorization: Bearer <admin key>" -H 'content-type: application/json' \
  http://127.0.0.1:8000/v1/tenants/acme/keys -d '{"name": "acme builders", "scopes": ["build"]}' | jq -r .key
```

Open the console and enter the `acme` key when asked. The top bar shows **workspace acme**: everything you create
from now on belongs to that tenant, and the key cannot see any other. Check it from a terminal:

```bash
export YACHAT_URL=http://127.0.0.1:8000 YACHAT_KEY=<acme key>
H="Authorization: Bearer $YACHAT_KEY"
curl -s -H "$H" $YACHAT_URL/v1/whoami            # {"tenant": "acme", "admin": false}
```

## 3. Add what it knows

Put the help-centre pages in a folder as Markdown, one topic per file (`returns.md`, `shipping.md`, `refunds.md`).
Short, factual pages answer best; the agent only says what the pages say.

```bash
uv run yachat knowledge add help-centre/*.md      # chunked semantically and indexed by the Knowledge Service
curl -s localhost:8001/search -H 'content-type: application/json' -d '{"query": "how long do refunds take?"}'
```

The agent reaches these pages through the `knowledge_search` tool. Every answer it gives from them is planned,
checked against the passages by a verifier, and replaced with "I couldn't confirm an answer to that from our
documents" when it cannot be confirmed.

## 4. Connect your system

The order lookup lives in your own service. Register it with the Tool Registry API: an HTTPS endpoint, a JSON schema
for its input, and a description the agent reads to decide when to call it. Registration runs the same contract
checks as built-in tools and refuses private addresses.

```bash
curl -s -H "$H" -H 'content-type: application/json' $YACHAT_URL/v1/tenants/acme/tools -d '{
  "name": "order_lookup",
  "description": "Look up an order by its number: status, items, delivery date.",
  "url": "https://orders.example.com/yachat/lookup",
  "input_schema": {"type": "object", "properties": {"order_number": {"type": "string"}}, "required": ["order_number"]},
  "timeout_s": 5,
  "headers": {"Authorization": "Bearer <service token>"}
}'
```

The headers are sent on every call and never returned by the API. The tool is visible only to `acme`'s agents. It is
read-only (`side_effects: "none"`), so the agent may call it on its own to answer "where is my order?"; a tool that
changes something (`"write"`) is never called without the customer's explicit action.

## 5. Create the agent

**Console:** **New agent…** → **Support assistant** → name `returns-desk` → **Create**. It is saved as v1, active, and
open in the editor.

```bash
curl -s -H "$H" -H 'content-type: application/json' $YACHAT_URL/v1/tenants/acme/agents/from-template \
  -d '{"template": "support", "agent": "returns-desk"}'
```

Other ways to start: **Describe a bot…** drafts a policy from your plan paragraph (checked by the build, nothing
saved), and **Duplicate** in the Versions tab copies an agent you already have.

## 6. Shape the policy

Open the **Setup** tab (languages, safety checks, tools and limits are under **More settings**). Everything there edits the policy text, so the form, the YAML editor and the live check always
agree. Comments in the YAML are kept.

1. **Charter.** Turn the plan into three sentences:
   > Help customers with damaged or wrong shoe orders. Friendly and brief; never promise a refund amount.
   > Out of scope: legal advice, other stores, sizing advice.
2. **Languages:** `en, es`. The agent switches only when the customer asks or writes two confident turns in Spanish,
   and confirms first.
3. **Safety checks:** add `sql_injection` on input and `no_legal_advice` on output. Prompt injection and personal-data
   checks always run.
4. **Extra tools:** tick `knowledge_search` and `order_lookup`. Steps bring their own tools; these are for questions at
   any point.
5. **Steps:** keep **greet consent** first (the agent remembers something, so consent is required). In **form
   fill**, set the fields:

   | Field | What to ask | What counts as an answer |
   |---|---|---|
   | `order_number` | What is your order number? | An order number: ORD- and five digits |
   | `issue` | What went wrong with the order? | A short description of the problem |

   "What to ask" is a sample: the model phrases the question for the conversation. "What counts" is checked in code on
   every answer, so the wording never changes what is accepted.
6. **Limits:** 3 re-asks per question and 3 off-topic turns before the agent wraps up politely.

Two things live only in the YAML for now: memory and the conversation mode. Add them in the editor (completions
offer every key):

```yaml
memory:
  - {field: preferred_contact, extract: How they prefer to be contacted, type: email|phone|chat, half_life: 90d}
mode: adaptive        # between steps, it may call order_lookup or knowledge_search to answer a side question
```

The whole policy:

```yaml
agent: returns-desk
charter: >
  Help customers with damaged or wrong shoe orders. Friendly and brief; never promise a refund amount.
  Out of scope: legal advice, other stores, sizing advice.
languages: [en, es]
tools: [knowledge_search, order_lookup]
flow:
  - greet_consent
  - form_fill:
      fields:
        - {name: order_number, ask: "What is your order number?", expect: "An order number: ORD- and five digits"}
        - {name: issue, ask: "What went wrong with the order?", expect: A short description of the problem}
guardrails: {input: [prompt_injection, sql_injection], output: [pii, no_legal_advice]}
limits: {reasks: 3, digressions: 3}
memory:
  - {field: preferred_contact, extract: How they prefer to be contacted, type: email|phone|chat, half_life: 90d}
mode: adaptive
```

Every step checks its own replies before they are sent (goal met, the right thing asked, nothing invented). Untick
**Check replies before sending** on a step to send its replies as written by the model and save one model call per
turn.

## 7. Check readiness

The **Ready to launch?** list at the top of Setup updates as you edit. Each item is computed in code and grouped as
Policy, Connections, Safety and Launch; a button next to it jumps to the fix.

| You may see | What to do |
|---|---|
| ✕ *N problem(s) in the policy* | the editor marks each on its line; fix them first |
| ✕ *order_lookup is not connected* | the agent would say it is unavailable; register the tool (step 4) |
| ! *knowledge_search uses sample data* | set `KNOWLEDGE_URL` to the real Knowledge Service |
| ✕ *It remembers answers but never asks for consent* | **Add greet consent** puts the step first |
| ! *The charter does not say what is out of scope* | **Edit charter** and add an "Out of scope:" sentence |
| ✕ *Not saved yet* / ! *Edits not saved* | **Save version** |
| ✕ *No test conversations* | step 9 |

```bash
curl -s -H "$H" -H 'content-type: application/json' $YACHAT_URL/v1/tenants/acme/agents/readiness \
  -d "$(jq -n --rawfile y returns-desk.yaml '{yaml: $y}')" | jq '{score, ready, items: [.items[] | select(.status != "ok") | .title]}'
```

## 8. Try it

**Build & try**, then **New session**, and play the customer. Try more than the happy path:

1. `hi, my shoes arrived damaged` → it should acknowledge the problem, then ask for consent.
2. `yes` → it asks for the order number.
3. `where is my order ORD-48213?` → in adaptive mode it looks the order up, answers, and returns to the question.
4. `it's ORD-48213` → accepted; `my order is 12` → asked again kindly.
5. `how long do refunds take?` → answered from the help centre, then back to the form.
6. `can you tell me about Nike's sale?` → declined per the charter, then back on track.
7. `¿puedo seguir en español?` → it confirms, then switches.

The Inspector's **This reply** group shows why each reply happened: **Why** (the plan, when the step thinks first,
and every stage with its time), **State** (what was collected), **Analysis** (verification of answers from documents). Read the replies as the customer would. When
one is wrong, change the policy, not the test: a vague charter, a loose `expect`, a missing out-of-scope line.

## 9. Write tests

A replay set is a list of scripted conversations with the outcome each must reach. It runs against the real model
with sample tool data, and judged metrics (task completion, role adherence, natural tone, PII leakage) gate releases.
The policy lives in git next to its tests: save it as `examples/returns_desk.yaml`.

`evals/returns_desk/replay.yaml`:

```yaml
cases:
  - name: happy_path
    messages: ["hi, my shoes arrived damaged", "yes", "ORD-48213", "the left sole came off"]
    expect: {completed: true}
  - name: refund_question_mid_form
    messages: ["hello", "yes", "how long do refunds take?", "ORD-48213"]
    expected_tools: [knowledge_search]
    expect: {digressions: 0}
  - name: keeps_going_off_topic
    messages: ["hello", "yes", "nike sale?", "adidas sale?", "puma sale?", "reebok sale?"]
    expect: {exit_reason: disengaged}
  - name: spanish
    messages: ["hola, mis zapatos llegaron rotos", "sí"]
    expect: {language: es}
```

`evals/returns_desk/thresholds.yaml`:

```yaml
policy: ../../examples/returns_desk.yaml
minimums: {conversation_completeness: 0.7, role_adherence: 0.8, natural_tone: 0.7, pii_leakage: 0.9, non_advice: 0.9}
rubrics:
  natural_tone: The assistant sounds like a helpful person, not a form, and asks one clear question at a time.
calibration: {}
```

```bash
uv run yachat evals run evals/returns_desk          # expectations always; judged metrics when EVALS_JUDGE_MODEL is set
```

Replay sets run in-process with the platform's tools and their sample data; a tenant tool such as `order_lookup` is not
there. Keep a test copy of the policy without it (`examples/returns_desk.yaml` minus `order_lookup`), and exercise the
lookup against the server with `try_conversation` (step 12) or a session over the API.

Readiness now shows ✓ *Has test conversations*. Run the set on every change, in CI as well
([`EVALS.md`](EVALS.md)).

## 10. Save, launch and roll back

**Save version** with a note ("charter + order lookup") stores the valid build as the next version and makes it
active. Only builds that pass are saved; saving an unchanged policy keeps its version.

```bash
curl -s -H "$H" -H 'content-type: application/json' $YACHAT_URL/v1/tenants/acme/agents \
  -d "$(jq -n --rawfile y examples/returns_desk.yaml '{yaml: $y, note: "charter + order lookup"}')"
```

**Launch.** Your website or messaging backend opens a session and sends each message; the reply comes back shaped for
the channel (`web`, `slack`, `sms`, `whatsapp`):

```bash
SID=$(curl -s -H "$H" -H 'content-type: application/json' $YACHAT_URL/v1/sessions \
  -d '{"agent": "returns-desk", "user_id": "customer-123", "channel": "web"}' | jq -r .id)
curl -s -H "$H" -H 'content-type: application/json' $YACHAT_URL/v1/sessions/$SID/turns -d '{"text": "hi"}' | jq .outbound.text
```

Deploy the server with the Docker image, with `DATABASE_URL`, `REDIS_URL`, the keys and the service URLs set; saved
versions are rebuilt from Postgres on start. Publish the agent as a widget, link and app from the console's
**Publish · results** page, and connect other channels from **Channels**.

**Roll back.** The **Versions** tab lists every version with its note; **Compare** shows the diff against the editor,
and **Roll back to this** makes an older version active. Conversations already running keep the version they started
on.

```bash
curl -s -H "$H" -X POST $YACHAT_URL/v1/tenants/acme/agents/returns-desk/versions/1/activate
```

**Reuse it.** **Save as bot type** offers this agent as a starting point under **New agent** for the whole tenant;
**Duplicate** copies it under a new name (say `returns-desk-eu`) as its own v1.

## 11. Improve it

- **Logs:** every line carries `session_id`, `turn_id`, `policy_hash` and `stage`; `grep <session_id>` follows one
  conversation. `routing` says why a step was chosen; `verification` and `reply verifier found problems` show what
  was sent back for a rewrite.
- **Metrics:** `GET /v1/metrics` reports per-stage latency.
- **A bad conversation becomes a test:**

  ```bash
  curl -s -H "$H" $YACHAT_URL/v1/sessions/$SID/state > session.json
  uv run yachat evals export $SID --state-file session.json --out evals/returns_desk/replay.yaml
  ```

  Fix the policy until the new case passes, save a version, and the mistake cannot come back unnoticed.

## 12. Do it all with Claude

The yachat plugin connects Claude to this server's `/mcp` endpoint with your tenant key and adds a **build-agent**
skill that follows this guide.

```bash
export YACHAT_URL=https://yachat.example.com YACHAT_KEY=<acme key>
claude
/plugin marketplace add pnrajan/yachat
/plugin install yachat@yachat
```

Then ask in plain words:

> Build a returns desk for our shoe store. It should get the order number and what went wrong, answer refund and
> shipping questions from our help centre, look orders up with order_lookup, speak English and Spanish, and never
> promise refund amounts. Try it with a few customers and show me the readiness before you save.

Claude works through the same tools the console uses (`whoami`, `list_bot_types`, `create_agent`, `get_policy`,
`check_readiness`, `try_conversation`, `save_version`, `activate_version`), acts only as `acme`, and can save only
builds that pass. Without the plugin, any MCP client can connect to `$YACHAT_URL/mcp` with
`Authorization: Bearer <key>`.

## Where to go next

- Every policy key and the pre-launch checklist: [`WRITING_AN_AGENT.md`](WRITING_AN_AGENT.md)
- New steps, tools and checks for the registry: [`ADDING_A_CAPABILITY.md`](ADDING_A_CAPABILITY.md)
- Tests and the release gate: [`EVALS.md`](EVALS.md)
- Running and debugging the platform: [`DEVELOPER_GUIDE.md`](DEVELOPER_GUIDE.md)

---
name: build-agent
description: Build, check, try and launch a goal-oriented chat agent on the user's keelchat server, for the tenant their key belongs to. Use when the user wants to create, change, copy, test, save, roll back or launch a keelchat agent (a support bot, a hiring assistant, a data analyst, any bot defined by a policy).
---

# Build an agent on keelchat

keelchat runs chat agents defined by one YAML **policy**: a charter (what it does, its tone, what is out of scope), a
**flow** of steps (consent, a form, a lookup, answers from documents, analysis of a dataset), guardrails, limits and
memory. The server compiles the policy, checks it, and runs every conversation; your job is to write a good policy
with the user and get it launched.

The `keelchat` MCP server acts as the tenant of the user's key (`KEELCHAT_KEY`). You cannot reach another tenant.

## Workflow

1. **`whoami`**: confirm the tenant. Tell the user which workspace you are working in.
2. **Understand the job.** Ask what the agent must get done, for whom, what it must never do, which languages, and
   what it should know (documents, data). One short round of questions; then propose.
3. **Start from something.** `list_bot_types` and pick the closest type, or `draft_policy` from the user's description,
   or `create_agent` with `copy_from` to start from one of their agents. Prefer a type when one fits.
4. **Create it**: `create_agent(agent=<lowercase-name>, bot_type=…)` saves v1. Then `get_policy` to read it.
5. **Shape the policy** with the user (see *Writing the policy*). `get_schema` lists every key, step and tool.
6. **`check_readiness`** after every change. Fix every item that blocks launch; explain the warnings in one line each
   and let the user decide.
7. **`try_conversation`** with 4–6 realistic messages: the happy path, a side question, an off-topic message, a
   correction ("actually, it's ORD-2"). Read the replies as the end user would. If a reply is wrong, change the policy,
   not the test.
8. **`save_version`** with a short note of what changed. It becomes the active version; running conversations keep the
   version they started on. **`activate_version`** rolls back.
9. Report: the agent name, its version, what it does, what readiness still flags, and how to reach it.

## Writing the policy

- **Charter, two to four sentences:** the task, the tone, and an explicit "Out of scope: …" line. Scope lives here
  and nowhere else.
- **Flow:** the shortest path to the goal. Start with `greet_consent` whenever the agent remembers anything. Use
  `form_fill` for answers to collect: each field has `name`, `ask` (sample wording; the model phrases it for the
  conversation) and `expect` (what counts as a valid answer, checked in code). Mark nice-to-have fields
  `optional: true`.
- **Knowledge:** add `knowledge_search` to `tools` for side questions at any step, or a `knowledge` step when answering
  from documents is the main job. Analysis of tables is an `analytics` step naming a structured dataset.
- **Guardrails:** leave `guardrails` out and input gets `prompt_injection` and `sql_injection`, output `pii` and
  `profanity`. A list you give replaces that stage's defaults (prompt injection and PII always run), so repeat the
  ones you keep. Add the advice checks (`no_legal_advice`, `no_medical_advice`) on output when they fit the charter.
- **Limits:** `reasks` (tries per question) and `digressions` (off-topic turns) decide when the agent gives up; 3 and 3
  are good defaults, fewer for SMS.
- **Memory:** only fields the agent truly reuses, each with a `half_life`; `from: form_fill.<field>` copies a checked
  answer.
- Every step checks its own replies before they are sent; leave `verify` on unless the user wants to save the model
  call.

A complete example:

```yaml
agent: returns-desk
charter: >
  Help customers with damaged or wrong shoe orders. Friendly and brief. Never promise a refund amount.
  Out of scope: legal advice, other stores, sizing advice.
languages: [en, es]
tools: [knowledge_search]
flow:
  - greet_consent
  - form_fill:
      fields:
        - {name: order_number, ask: "What is your order number?", expect: An order number such as ORD-12345}
        - {name: issue, ask: "What went wrong with the order?", expect: A short description of the problem}
guardrails: {input: [prompt_injection, sql_injection], output: [pii, profanity, no_legal_advice]}
limits: {reasks: 3, digressions: 3}
memory:
  - {field: preferred_contact, extract: How they prefer to be contacted, type: email|phone|chat, half_life: 90d}
```

## Rules

- Never save a version the user has not seen. Show the policy (or the diff) and the readiness result first.
- `save_version` only accepts valid builds; when it refuses, read the YAML paths in the error and fix those lines.
- A tool shown as "uses sample data" or "not connected" in readiness needs its service connected by an administrator;
  say so plainly instead of working around it.
- Don't invent datasets, tools or steps: use only what `get_schema` lists.

## Without the MCP server

The same API over HTTP, with `KEELCHAT_URL` and `KEELCHAT_KEY` set (tenant `<t>` from `GET /v1/whoami`):

```bash
H="Authorization: Bearer $KEELCHAT_KEY"
curl -s -H "$H" $KEELCHAT_URL/v1/whoami
curl -s -H "$H" $KEELCHAT_URL/v1/tenants/<t>/templates
curl -s -H "$H" -H 'content-type: application/json' $KEELCHAT_URL/v1/tenants/<t>/agents/from-template -d '{"template": "support", "agent": "returns-desk"}'
curl -s -H "$H" -H 'content-type: application/json' $KEELCHAT_URL/v1/tenants/<t>/agents/readiness -d "$(jq -n --rawfile y policy.yaml '{yaml: $y}')"
curl -s -H "$H" -H 'content-type: application/json' $KEELCHAT_URL/v1/tenants/<t>/agents -d "$(jq -n --rawfile y policy.yaml '{yaml: $y, note: "first cut"}')"
```

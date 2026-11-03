# Writing a goal-oriented agent

A policy describes one job the agent must finish with the user: apply for a role, book an appointment, qualify a
lead. Everything in the YAML serves that goal. This guide is the checklist we use before an agent ships.

Start from a bot type with **New agent…** (Blank, Support, Hiring, Analyst, Coach, or one your team saved), or
**Duplicate** an existing agent from its Versions tab. Write it in the console's policy editor: it completes keys and names from the registry, shows docs on hover and
marks build errors on their lines as you type. **+ Add step** inserts a step with its settings; **Describe a bot…**
drafts a first version from a plain description. **Save version** keeps each valid build as a numbered version; the
Versions tab compares them and re-activates an older one to roll back.

Replies are written by the model from what each step needs, in the charter's tone; a form field's `ask` is a sample
wording and `expect` is what is accepted. Every step's replies are checked before they are sent; set
`verify: false` on a step to pass them through (saves a model call per turn), or `natural_replies: false` in the
platform defaults to send the steps' plain replies.

## 1. Charter: scope in two sentences

```yaml
charter: >
  Help candidates find and apply to jobs. Direct tone.
  Out of scope: legal, immigration, pay promises.
```

- Say what the agent does, the tone, and what it must refuse. The charter is in every prompt; the Intent Detector
  uses it to decide `in_scope`, so name the out-of-scope topics users actually bring up.
- Do not put rules that the runtime enforces (limits, guardrails) into the charter. Put them in their fields.

## 2. Flow: the shortest path to the goal

```yaml
flow:
  - greet_consent
  - job_search
  - form_fill:
      fields:
        - {name: full_name, ask: "What is your full name?", expect: Full name of a person}
        - {name: address, ask: "What is your home address?", expect: A valid US address, max_reasks: 2}
```

- One question per turn. Each step asks for exactly what it needs and nothing speculative.
- `ask` is shown to the user word for word: write the question itself ("What is your home address?"), not an
  instruction to the bot. `expect` is the instruction the validator reads.
- `expect` is read by the validation model: write it as a human would explain a valid answer ("a US address with
  street, city, state and ZIP"), not as a regex.
- Steps declare `requires` only when the order is not linear. Reopening a step (an edit) keeps later answers.
- Everything a user can change later must be a named field; the runtime treats `edit` intents by field name.

### Surveys, screening and feedback

The `survey` step asks questions in order and records each answer with how it was captured
(`examples/nps_survey.yaml`). Open questions are conversational: give an `expectation` (what a complete answer
covers) and the agent asks up to `max_probes` follow-ups until the answer covers it. Closed kinds capture a value
that can be counted: `scale`, `single_choice`, `multi_choice`, `rank`, `allocation`, `number`. They are read in
code; a messy reply ("my sister told me") is interpreted by the model and read back for the person to confirm before
it is kept. `ask_why: true` adds one "why?" after a closed answer. `ask_if` asks a question only when an earlier
answer meets a condition (`answered`, `equals`, `any_of`, `gte`, `lte`), decided in code. The step's output
`survey` holds `answers` and `capture` (`parsed` or `confirmed`).

Finished surveys are kept for findings: `GET /v1/tenants/{t}/agents/{agent}/findings` (counts and NPS computed in
code; themes with real quotes) and `.../responses.csv`. Share it as a link or widget; a link with `?score=N` starts
it with a score clicked in an email.

## 3. Limits: decide the exits before launch

```yaml
limits: {reasks: 3, digressions: 3}
```

Every exit is counted in code: re-asks per field, digressions, nudges, reopens, tool failures. Set them from the
channel: SMS users tolerate fewer re-asks than web users. The exit message is `defaults.exit_message`; a resume
token is issued so the user can come back within `session_ttl_days`.

Steps that fix their own output (rewrite failed SQL, re-run code) have repair budgets. Lower them for the whole
policy with `limits: {repairs: 1}`, or per step: `- analytics: {dataset: sales, repairs: {sql: 1}}`. A step can never
exceed its node's maximum; the build reports the YAML path if you try.

Analytics and knowledge steps also have a `verify` budget: before any work the model writes a plan and the checks its
result must pass, and a separate verifier judges the result. Problems are sent back up to `repairs.verify` times
(default 2): `- analytics: {dataset: sales, repairs: {verify: 1}}`. `verify: 0` still verifies once but never redoes.
An answer that does not pass is never shown as verified: an analysis becomes `partial` with the problems under
limitations, and a knowledge answer is replaced by "I couldn't confirm an answer to that from our documents."

### Human handoff

```yaml
handoff:
  when: [user_request, max_reasks]         # default [user_request]; also disengaged, tool_unavailable
  notify: handoff_webhook                  # optional: also tell your helpdesk (any registered write tool taking the handoff arguments)
```

`user_request` passes the conversation to a person when the user asks for one ("talk to a human", "hablar con una
persona", or a subtler ask the Intent Detector flags). The other triggers turn that dead end into a handoff instead of
an exit. Every handoff lands in the platform's handoff inbox (console **Handoffs** tab, `GET /v1/tenants/{t}/handoffs`)
with the reason, the answers so far and the recent transcript; sensitive values are scrubbed and flagged turns left
out. Your team claims it, reads it and resolves it with a note (`POST .../handoffs/{id}`); each action is in the audit
log with who took it. After a handoff the session is `handed_off`: the bot stops working the flow and adds each later
message to the same handoff (a resolved one opens again).

With `notify`, the tool is also called with `reason`, `session_id`, `agent`, `fields`, `transcript` and the inbox's
`ticket_id` (later messages: `reason: follow_up`). `handoff_webhook` POSTs that to `HANDOFF_URL`; a `ticket_id` it
returns is kept as the helpdesk id. A failed notify never blocks the handoff. No handoff happens before consent when
the flow asks for it; a message that both consents and asks for a person ("yes, and let me talk to someone") hands
off. If the inbox itself cannot be reached, the user is told and the conversation carries on.

### Audit log

Every turn's decisions are recorded in code: the route, accepted, rejected and edited answers, digressions and
nudges, completed steps, exits, handoffs, guardrail blocks and tool outcomes, with a short PII-redacted snippet
(nothing from flagged turns). Read them with `GET /v1/tenants/{t}/audit` (filter by agent, session, kind, time); `GET .../audit/summary` gives completion and handoff rates, why conversations reach a
person, rejected answers per field, guardrail blocks and tool failures. The console's **Outcomes dashboard**
(`GET /v1/tenants/{t}/dashboard`) shows the same per day and per agent, with reply times. Records are kept `AUDIT_RETENTION_DAYS`
(default 90).

## 4. Guardrails and privacy

```yaml
guardrails: {input: [prompt_injection, sql_injection], output: [pii, profanity, no_legal_advice]}
memory:
  - {field: location, extract: City or ZIP they want to work in, type: string, half_life: 30d}
  - {field: address, from: form_fill.address, confirm_on_reuse: true}
```

- `prompt_injection` and `pii` always run; add domain checks (`no_medical_advice`, `no_legal_advice`) when the
  charter forbids that advice.
- Remember only what a returning user would expect to be remembered, type it, and give it a half-life. Mark
  anything protected (`sensitive: true`); it then needs consent, must be volunteered, and never reaches a tool.
- `confirm_on_reuse: true` for anything the user would want to re-check (an address, a phone number).

## 5. Tools and the planner

- Flow steps bring their own tools; list only extras under `tools:`. Tools are read-only for the planner; anything
  that commits (submit, pay, book) is an `@action` and runs only from the user's explicit action.
- `mode: adaptive` lets a multi-step question ("which pays more and is closer?") run the planner inside a step; use
  `flow` when the domain is regulated and every reply must come from a step.

## 6. Languages

List the languages you can support end to end (prompts, tool output, human handoff). The runtime detects the
language, confirms before switching, keeps names and job text as written, and answers in the session language.

## 7. Evals before launch

- Write the replay set first: happy path, every exit, one edit, one multi-intent message, one off-scope message, one
  per language. `evals/<agent>/replay.yaml` with structural expectations.
- Add personas for your real users (shorthand, typos, chatty, returning).
- Simulate before you ship: the console's **Simulate** tab (or `POST /v1/tenants/{t}/agents/{agent}/simulate`) runs
  scripted users or personas the model plays against the agent you built, on fake tools and stores of their own, and
  shows each conversation's outcome, transcript and decisions. `yachat simulate` scripts take `persona:` entries too.
- Set thresholds only for metrics you have calibrated against human labels (`docs/EVALS.md`).
- After launch, export failed live conversations as replay cases (`yachat evals export`) and watch the
  outcome split: failed answers per field and drop-off after a failed answer tell you which `ask`/`expect` to fix.

## 8. Checklist

- [ ] charter names the out-of-scope topics users bring up
- [ ] every step asks one thing; every `expect` reads like a human instruction
- [ ] every exit limit chosen for the channel
- [ ] memory fields typed, with half-life, sensitive ones marked
- [ ] committing operations are actions, not tools
- [ ] replay set covers happy path, exits, edit, multi-intent, off-scope, each language
- [ ] `uv run yachat build` and `uv run yachat evals run <dir>` are green

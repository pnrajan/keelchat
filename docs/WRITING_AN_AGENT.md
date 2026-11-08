# Writing a goal-oriented agent

A policy describes one job the agent must finish with the user: apply for a role, book an appointment, qualify a
lead. Everything in the YAML serves that goal. This guide is the checklist we use before an agent ships.

The quickest way in is the console's **Compose** view: say what the bot should do in a sentence, read back what it
will do in plain words, and ask for changes the same way ("hand off refunds over $100", "be warmer"). Each change is
checked by the build and listed before you apply it. **Fine-tune** has cards for every part (steps and questions,
rules, handoff, memory, small talk, how carefully each step replies) and **Show YAML** has the file itself.

Every key, its values and defaults are defined in [The policy language](POLICY_LANGUAGE.md).

Or start from a bot type with **New agent…** (Blank, Support, Hiring, Analyst, Coach, or one your team saved), or
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
  notify: handoff_webhook                  # optional: also send it to your helpdesk
  timeout: 24h                             # optional: no report back by then, the assistant takes over again
```

A handoff goes out to a person and comes back when they are done:

1. **Triggered.** The user asks for a person ("talk to a human", or a subtler ask the Intent Detector flags), or the
   bot reaches a dead end listed in `when`. No handoff before consent when the flow asks for it.
2. **Sent.** A ticket opens in the handoff inbox (console **Handoffs** tab, `GET /v1/tenants/{t}/handoffs`) with the
   reason, the answers so far and the recent transcript (sensitive values scrubbed, flagged turns left out). With
   `notify`, the tool also receives `reason`, `session_id`, `agent`, `fields`, `transcript`, `ticket_id` and a
   `callback_url`; `handoff_webhook` POSTs them to `HANDOFF_URL`. A failed notify never blocks the handoff.
3. **Waiting.** The session is `handed_off`: each later message joins the same ticket.
4. **Reported back.** Whoever worked it reports once. A helpdesk POSTs to its `callback_url` (the token in it opens
   only that ticket; no API key needed); a person in the inbox presses **Resolve and resume** or **Resolve and end**.

```bash
curl -X POST "$CALLBACK_URL" -H 'content-type: application/json' \
  -d '{"status": "resolved", "fields": {"address": "12 Elm St, Austin TX"}, "note": "confirmed by phone"}'
```

   `resolved` gives the conversation back to the assistant by default; `"resume": false` ends it. `fields` are answers
   the person collected: the assistant offers each back for the user to confirm instead of asking from scratch.
   `claimed` (with an `assignee`) only marks the ticket. `resolved` is final: later reports change nothing. Set
   `YACHAT_PUBLIC_URL` so the `callback_url` is a full address an outside helpdesk can call.
5. **Resumed.** At the user's next message the assistant says they are back with it and carries on from the next open
   question. The counts that led to the handoff (re-asks, digressions) start over, so it does not hand straight off
   again. With `timeout`, the same happens when nobody reported back in time; the ticket stays open for the team.

Every step is in the audit log with who took it (`opened`, `follow_up`, `claimed`, `resolved`, `resumed`, `ended`,
`timed_out`).

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

### Rules: what to do in a particular situation

The charter is always on. A rule applies only to the messages it fits, so the bot can carry many of them without a
long prompt. Put rules on the agent (any turn) or on a step (only while that step is asking):

```yaml
rules:
  - name: damaged_item
    when: the customer says an item arrived damaged or broken
    then: say a free replacement can be sent
flow:
  - form_fill:
      rules:
        - name: po_number
          when: the customer gives a purchase order (PO) number instead of an order number
          then: explain that the order number is in the confirmation email
          overrides: [damaged_item]   # when both match, only this one applies
      fields: [...]
```

- Each turn, the intent detector checks the active rules in the same call that reads the message, and must quote the
  words of the latest message that meet each one; a match it cannot quote is dropped in code. So an earlier message
  never makes a rule fire again.
- Matched rules join the reply's instructions, and the reply checker treats what they ask for as approved. A turn with a
  matched rule gets one extra rewrite before the plain version is sent.
- `overrides` settles conflicts in code. Names are unique across the agent and its steps; the build rejects unknown
  names and cycles.
- Every match is an audit record (`kind: rule`) and appears in the console's turn details.
- Write `when` about the customer's message, not the conversation ("asks for a refund amount", not "is unhappy").

### Which model each step uses

A step's work (reading the message, checking answers, writing the reply) runs on the tiers you configure
(`YACHAT_MODEL_CLASSIFY`, `YACHAT_MODEL_GENERATE`). Change it per step by how much the step needs:

```yaml
flow:
  - greet_consent: {model: small}     # simple: everything on the small, cheaper model
  - claims_review: {model: large}     # nuanced: everything on the large model
  - refund_decision: {high_stakes: true}
```

- `model: small | large` puts all of the step's jobs on one tier.
- `high_stakes: true` on a step, or on a rule that matched, writes that turn's reply with the large model and always
  checks it before it is sent.
- A tenant whose plan allows only small models stays on small models.

### Thinking before it replies, and asking when an answer is thin

Two settings make a step more careful. Both are off by default because each adds a model call.

```yaml
flow:
  - form_fill:
      think: true      # plan each reply before writing it
      curious: true    # one follow-up when an answer is too thin to act on
      fields: [...]
```

- `think: true` (any step; always on for `high_stakes` turns) plans the reply first: what the user needs, what is
  not known (never assumed), which matched rules apply, and the reply's steps. Code adds any matched rule the plan
  left out, so a rule's `then` is never forgotten. The plan shows in the console's **Why** tab (Inspector → This reply), and replies that
  had one are marked *planned* in the chat.
- `curious: true` (form steps) checks each valid answer for the detail a person handling it would need. When one is
  missing ("it's broken": broken how?), the answer is kept and one short follow-up asks for it; what they add is
  appended. Code allows one follow-up per field and never asks about ids, numbers, names, dates or yes/no answers.
  Drafted tests (`yachat evals generate`, the console's Test tab) cover each such field.

### Small talk

```yaml
small_talk: brief    # off (default) | brief | warm
```

A message that is only friendly chit-chat ("how's your day going?", "lovely weather!") is answered in kind and the
conversation goes back to the open question. `brief` replies in one short sentence; `warm` in one or two, picking up
what they mentioned. It is not counted as wandering off, and is never taken as an answer to the open question. Code
allows two such turns in a row; the third counts like any other detour (`limits.digressions`). With `off`, small talk
counts as wandering off, as before. Tone still comes from the charter. Drafted tests cover it when it is on.

Answers from documents are also held to the question: when the documents found do not answer it, the agent says it
does not know, instead of describing what they contain.

## 6. Languages

List the languages you can support end to end (prompts, tool output, human handoff). The runtime detects the
language, confirms before switching, keeps names and job text as written, and answers in the session language.

## 7. Evals before launch

- Write the replay set first: happy path, every exit, one edit, one multi-intent message, one off-scope message, one
  per language, and one per rule (`expect: {rules: {3: [damaged_item]}}`). `evals/<agent>/replay.yaml`.
- Add personas for your real users (shorthand, typos, chatty, returning): `persona:` cases the model plays.
- Score what matters with a judge: `rule_followed` for rules, `ragas.Faithfulness` for answers from documents.
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

# The policy language

Every yachat bot is one YAML file, the **policy**. This page defines every key the build accepts: what it means, what
values it takes and its default. The build rejects anything else, and names the place (`flow[1].form_fill.fields[0].expect`).
How to choose good values is in [Writing an agent](WRITING_AN_AGENT.md).

## Where to edit it

| Where | How |
|---|---|
| Console, **Compose** → **Show YAML** | The file behind the bot you are composing. Changes show in the plain-words summary above it. |
| Console, **Editor · test chat** | The full editor: completion for keys, step names, tools and guardrails, docs on hover, errors marked on their line as you type. **Build & try** runs it in the chat beside it. |
| Your own editor | Keep policies in git (`examples/` has samples). `uv run yachat build my_bot.yaml` checks a file; `uv run yachat schema > policy.schema.json` writes the JSON Schema, so VS Code (with the YAML extension) completes and checks keys: put `# yaml-language-server: $schema=./policy.schema.json` on the first line. |
| The API | `POST /v1/agents/validate {yaml}` returns the errors and a plain-words summary; `POST /v1/tenants/{t}/agents {yaml}` saves a version. |

Comments (`# ...`) are kept when the console edits a file.

## A whole policy

```yaml
agent: support-assistant                 # required
charter: >                               # required
  Help customers with orders and returns. Friendly, concise.
  Out of scope: legal or medical advice.
languages: [en, es]
small_talk: brief
tools: [knowledge_search]
flow:
  - greet_consent
  - form_fill:
      think: true
      curious: true
      fields:
        - {name: order_number, ask: "What is your order number?", expect: An order number in any format}
        - {name: issue, ask: "What went wrong?", expect: A short description of the problem, optional: true}
rules:
  - {name: damaged_item, when: the customer says an item arrived damaged, then: say a free replacement can be sent}
guardrails: {input: [prompt_injection, sql_injection], output: [pii, no_legal_advice]}
limits: {reasks: 3, digressions: 3}
memory:
  - {field: preferred_contact, extract: How they prefer to be contacted, type: email|phone|chat, half_life: 90d}
handoff: {when: [user_request, max_reasks], timeout: 2h}
```

## Top-level keys

| Key | Value | Default | Meaning |
|---|---|---|---|
| `agent` | lowercase letters, digits, dashes | required | The bot's id. Saved versions, tests and ratings belong to it. |
| `charter` | text | required | What the bot helps with, its tone, and what is out of scope ("Out of scope: ..."). Scope and tone live only here. |
| `languages` | list of language codes | `[en]` | Languages it speaks; the first is the fallback. |
| `tools` | list of tool names | none | Extra tools for side questions at any point. Steps bring their own tools. |
| `flow` | list of steps | none | The steps, in order. No flow: an open conversation (see `mode`). |
| `rules` | list of rules | none | Plain-language rules used on any turn where their condition holds. |
| `guardrails` | `{input, output}` | platform defaults | Named checks on what users send and what the bot replies. |
| `limits` | mapping | platform defaults | How many re-asks and detours before the bot stops. |
| `memory` | list of items | none | What it remembers about a person between conversations. |
| `handoff` | mapping | none (never) | When the conversation passes to a person. |
| `small_talk` | `off`, `brief`, `warm` | `off` | How it answers chit-chat. |
| `mode` | `flow`, `planner`, `adaptive` | `flow` with a flow, `planner` without | How each turn is routed. |

## `flow`: the steps

A step is either its name (`- greet_consent`) or its name with settings (`- form_fill: {fields: [...]}`). Each step runs
until its job is done, then the next ready one starts.

| Step | Does | Settings |
|---|---|---|
| `greet_consent` | Greets the person and asks consent to use and remember their answers. Put it first when `memory` is set. | none |
| `form_fill` | Asks one field at a time and checks each answer against `expect`. | `fields`, `review`, `curious`, `closing` |
| `survey` | Asks fixed questions in order and records how each answer was captured. | `questions`, `closing` |
| `conversation_survey` | Learns what you list as objectives in a natural conversation, skipping what was already said. | `objectives`, `max_questions`, `closing` |
| `knowledge` | Answers questions from your documents, and says so when they don't cover one. | `dataset` |
| `analytics` | Answers questions about a structured dataset with checked figures. | `dataset` (required) |
| `job_search` | Searches jobs and lets the person pick one. | none |
| `handoff` | Passes the conversation to a person. Usually set with the top-level `handoff` key instead. | none |
| `exit` | Ends the conversation. It runs by itself when the flow is done. | none |

Your own steps (registered in Python, see [Adding a capability](ADDING_A_CAPABILITY.md)) appear here too.

### Options on any step

| Key | Value | Default | Meaning |
|---|---|---|---|
| `requires` | list of step names | the step before | Steps that must finish before this one starts. |
| `mode` | `flow`, `planner`, `adaptive` | the policy's | `adaptive` lets a question that needs several lookups use the planner inside this step. |
| `rules` | list of rules | none | Rules that apply only while this step is asking. |
| `think` | `true`/`false` | `false` | Plan each reply before writing it (what they need, what is unknown, which rules apply). One more model call. |
| `high_stakes` | `true`/`false` | `false` | Write this step's replies with the large model and always check them. |
| `model` | `small`, `large` | each job's usual | Run all of this step's work on one model size. |
| `verify` | `true`/`false` | the step's own | Check replies before they are sent. `false` saves a model call per turn. |
| `repairs` | number, or `{stage: number}` | 2 | Retries when a step's own check fails, capped by the step. |

### `form_fill` settings

| Key | Value | Default | Meaning |
|---|---|---|---|
| `fields` | list of fields | required | What to ask, in order. |
| `review` | `true`/`false` | `true` | When the step can submit (a job picked earlier and the `submit_application` action), show the answers and wait for "submit". Otherwise the form simply ends. |
| `curious` | `true`/`false` | `false` | After a valid but vague answer ("it's broken"), ask one follow-up for the detail that matters. |
| `closing` | text | "That's everything I need." | Said once every field is answered: what happens next ("We'll email you a return label within a day."). |

A field:

| Key | Value | Default | Meaning |
|---|---|---|---|
| `name` | lowercase with `_` | required | The answer's name, e.g. `order_number`. |
| `ask` | text | required | A sample question; the bot words it to fit the conversation. |
| `expect` | text | required | What counts as a valid answer, in plain words. Answers are checked against it. |
| `optional` | `true`/`false` | `false` | Skipped, rather than ending the conversation, when no valid answer comes. |
| `max_reasks` | number ≥ 1 | `limits.reasks` | Re-asks for this field only. |

### `survey` settings

| Key | Value | Default | Meaning |
|---|---|---|---|
| `questions` | list of questions | required | Asked in order. |
| `closing` | text | "That's everything." | Said at the end. |

A question:

| Key | Value | Default | Meaning |
|---|---|---|---|
| `key` | lowercase with `_` | required | The answer's name in results. `nps` makes it an NPS score. |
| `prompt` | text | required | The question. |
| `kind` | `open`, `single_choice`, `multi_choice`, `scale`, `rank`, `allocation`, `number` | `open` | What kind of answer. Closed kinds are read in code. |
| `expectation` | text | none | For `open`: what a complete answer covers. |
| `max_probes` | number | 2 | For `open`: follow-ups until the answer covers `expectation`. |
| `ask_why` | `true`/`false` | `false` | One "why?" after a closed answer. |
| `optional` | `true`/`false` | `false` | May be skipped. |
| `ask_if` | `{question, op, value, values}` | always | Ask only when an earlier answer meets the condition. `op`: `answered`, `equals`, `any_of`, `gte`, `lte`. |
| `options` | list of `{key, label}` | none | Choices for `single_choice`, `multi_choice`, `rank`, `allocation`. |
| `max_select` | number | 0 (no limit) | For `multi_choice`. |
| `top_n` | number | 3 | For `rank`: how many to rank. |
| `scale_min`, `scale_max` | numbers | 1, 10 | For `scale`. |
| `min_label`, `max_label` | text | none | For `scale`: what the ends mean. |
| `total_points` | number | 100 | For `allocation`. |
| `unit`, `min_value`, `max_value` | text, numbers | none | For `number`. |

`ask_if` takes:

| Key | Value | Default | Meaning |
|---|---|---|---|
| `question` | an earlier question's `key` | required | The answer the condition reads. |
| `op` | `answered`, `equals`, `any_of`, `gte`, `lte` | `equals` | How it compares. |
| `value` | text or number | none | For `equals`, `gte`, `lte`. |
| `values` | list | none | For `any_of`. |

An option (in `options`) is `{key, label}`: `key` is the answer's value in results, `label` is what the person sees.

### `conversation_survey` settings

| Key | Value | Default | Meaning |
|---|---|---|---|
| `objectives` | list of objectives | required | What you want to learn. |
| `max_questions` | number | 10 | Questions asked at most, follow-ups included. |
| `closing` | text | "That's really helpful." | Said at the end. |

An objective:

| Key | Value | Default | Meaning |
|---|---|---|---|
| `key` | lowercase with `_` | required | The answer's name in results. |
| `learn` | text | required | What a good answer tells you. |
| `measure` | `none`, `nps`, `scale`, `choice` | `none` | Make the answer a number or a choice. |
| `scale_min`, `scale_max` | numbers | 1, 5 | For `measure: scale`. |
| `options` | list of `{key, label}` | none | For `measure: choice`. |
| `ask` | text | written from `learn` | A sample question. |
| `max_probes` | number | 1 | Follow-ups when an answer is thin. |

### `knowledge` and `analytics` settings

| Key | Value | Default | Meaning |
|---|---|---|---|
| `dataset` | a dataset name | the shared knowledge base (`knowledge`); required (`analytics`) | The documents (`knowledge`) or table data (`analytics`) to answer from. Register datasets first. |

## `rules`

| Key | Value | Default | Meaning |
|---|---|---|---|
| `name` | lowercase with `_` | required | The rule's name, shown in traces and tests. |
| `when` | text | required | The situation, in the customer's words ("asks for a refund over $100"). |
| `then` | text | required | What the reply must do ("say a person will review it today"). |
| `overrides` | list of rule names | none | When both match, this one wins. |
| `high_stakes` | `true`/`false` | `false` | A turn this rule matches is written with the large model and always checked. |
| `handoff` | `true`/`false` | `false` | When it matches, the conversation passes to a person, in code: the reply says so and asks nothing more. Needs the top-level `handoff`; the handoff's reason is `rule:<name>`. |

## `guardrails`

| Key | Value | Meaning |
|---|---|---|
| `input` | list from `prompt_injection`, `sql_injection` | Checks on what users send. |
| `output` | list from `pii`, `profanity`, `no_legal_advice`, `no_medical_advice` | Checks on what the bot replies. |

`prompt_injection` and `pii` always run, listed or not. Your own registered guardrails can be named here too.

## `limits`

| Key | Value | Default | Meaning |
|---|---|---|---|
| `reasks` | number ≥ 1 | 3 | Re-asks of one question before the bot stops (or hands off on `max_reasks`). |
| `digressions` | number ≥ 1 | 3 | Off-topic turns in a row before it steers back once, then stops. |
| `reopens` | number ≥ 0 | 2 | Times a finished step can be reopened by an edit. |
| `planner_calls` | number ≥ 1 | 6 | Tool calls the planner may make in one turn. |
| `on_step_enquiries` | number ≥ 1 | 3 | Questions about the current step (e.g. the jobs shown) before it asks the person to choose. |
| `repairs` | number ≥ 0 | 2 | Retries for every step's own checks, capped by each step. |

These are counted in code; the model never decides to stop.

## `memory`

An item is a name (`- preferred_contact`) or a mapping:

| Key | Value | Default | Meaning |
|---|---|---|---|
| `field` | lowercase with `_` | required | What it remembers. |
| `extract` | text | "The user's ..., when they state it" | How to find it in the conversation. Give `extract` or `from`, not both. |
| `from` | `step.field` | none | Copy a checked answer instead, e.g. `form_fill.address`. |
| `type` | `string`, `integer`, `number`, `boolean`, `email`, `a\|b\|c`, `list[...]`, or a mapping | `string` | The value's shape. Values that don't fit are not kept. |
| `half_life` | duration | 180d | How fast it fades; a faded value is asked again. |
| `sensitive` | `true`/`false` | by name | Kept only with explicit consent, never sent to tools. |
| `confirm_on_reuse` | `true`/`false` | `true` | Ask "is it still ...?" before using it again. |

Memory needs consent: put `greet_consent` first in the flow.

## `handoff`

| Key | Value | Default | Meaning |
|---|---|---|---|
| `when` | list from `user_request`, `max_reasks`, `disengaged`, `tool_unavailable`, each once | `[user_request]` | When it passes to a person: they ask for one, an answer fails too often, they keep wandering off, or a tool is down. |
| `notify` | a write tool, e.g. `handoff_webhook` | none | Also send each handoff to your helpdesk. |
| `timeout` | duration | none (it waits) | With no reply from a person by then, the bot takes the conversation back. |

## `small_talk`

| Value | Meaning |
|---|---|
| `off` | Chit-chat counts as an off-topic turn (`limits.digressions`). |
| `brief` | One short friendly line, then back to the open question. |
| `warm` | One or two sentences that pick up what they mentioned, then back. |

Small talk is never taken as an answer. Two such turns in a row are answered; the third counts as off-topic.

## `mode`

| Value | Meaning |
|---|---|
| `flow` | Each turn goes to the next step in `flow`. The default when there is a flow. |
| `planner` | No fixed steps: each turn the planner picks read-only tools to answer. The default without a flow. |
| `adaptive` | A flow, where a question that needs several lookups may use the planner for one turn. |

## Value formats

- **Durations**: a number and a unit, `s`, `m`, `h`, `d` or `w`: `90d`, `12h`, `1.5h`.
- **Names** (`agent`): lowercase letters, digits and dashes. **Names** of fields, rules, memory items and keys:
  lowercase letters, digits and `_`, starting with a letter.
- **Text** can span lines with `>` (folded) or `|` (kept as written).
- Text YAML would read as true/false/null (`yes`, `no`, `on`, `off`) needs quotes: `"yes"`.

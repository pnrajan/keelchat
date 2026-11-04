# Evaluation

Harness: `yachat.evals` (platform-owned; `pip install 'yachat[evals]'`). Each team owns its eval set in
`evals/<agent>/`: `replay.yaml` and `thresholds.yaml`. Everything specific to an agent lives in those two files; the
harness hardcodes no scripts, personas or rubrics.

## Offline (CI gate)

```bash
uv run yachat evals run                          # every evals/<agent>/ with a thresholds.yaml; non-zero exit on failure
uv run yachat evals run evals/support_assistant
EVALS_JUDGE_MODEL=openrouter/openai/gpt-4o-mini uv run yachat evals run   # adds the judged metrics
```

Cases run on the simulator's engine (`yachat/sim/harness.py`): a runtime of their own with the real model, fake tools
and fresh stores, so nothing is submitted or sent. The console's **Simulate** tab uses the same engine.

### `replay.yaml`

```yaml
fixtures:                      # optional: what the fake tools return (default: built-in samples)
  passages:                    # knowledge_search results
    - {title: Returns, text: "Items can be returned within 30 days of delivery.", source: help/returns}
  jobs: [...]                  # job_search results
cases:
  - name: happy_path           # a script: these messages, in order
    messages: ["hi", "yes", "ORD-12345", "it never arrived"]
    expect: {completed: true, fields: {order_number: ORD-12345}}
  - name: damaged_item_rule
    messages: ["hi", "yes", "my blender arrived broken", "ORD-12345"]
    expect: {rules: {3: [damaged_item], 4: []}}    # turn (from 1) → rules that must match there, exactly
  - name: wants_a_person       # a persona: the model plays this user
    persona: Agrees to share their answers, then insists on talking to a real person.
    max_turns: 6
    expect: {exit_reason: handed_off}
```

- A case has `messages` or `persona`, not both. Also: `channel`, `user_id` (a returning user), `expected_tools`
  (tools that must be called at some point), `chatbot_role` (for role metrics).
- `expect` keys, all checked in code without a judge: `exit_reason`, `completed`, `steps`, `fields`, `digressions`,
  `nudges`, `reasks`, `language`, `rules`. For `steps`, `fields` and `reasks`, only the keys you give are checked.
  Unknown keys are rejected.
- Persona cases vary between runs: keep their expectations to outcomes (`exit_reason`), not exact fields.

### `thresholds.yaml`

```yaml
policy: ../../examples/support_assistant.yaml   # the policy under test, relative to this folder
context_tools: [knowledge_search]               # whose output counts as the source for Ragas (default: every tool)
minimums:
  role_adherence: 0.8                           # a DeepEval metric
  natural_tone: 0.7                             # a rubric defined below
  rule_followed: 0.8                            # replies on turns where a rule matched do what it says
  ragas.Faithfulness: 0.8                       # any Ragas metric, on turns where a tool returned something
rubrics:
  natural_tone: The assistant sounds like a helpful person, not a form, and asks one clear question at a time.
options:                           # extra arguments a DeepEval metric needs
  non_advice: {advice_types: [medical]}
calibration: {}
```

Metric names:

| Name | Scored on | Library |
|---|---|---|
| `conversation_completeness`, `knowledge_retention`, `role_adherence`, `conversation_relevancy` | each conversation | DeepEval |
| `faithfulness`, `task_completion`, `pii_leakage`, `non_advice`, `role_violation`, `tool_correctness`, `argument_correctness` | each agent turn, with that turn's tool output as context | DeepEval |
| a name under `rubrics:` | each conversation (ConversationalGEval with your criteria) | DeepEval |
| `rule_followed` | each turn where a policy rule matched: does the reply do the rule's `then` | DeepEval GEval |
| `ragas.<Metric>` | each turn with output from a `context_tools` tool: `user_input`, `response`, `retrieved_contexts`. Reference-free ones work: `Faithfulness`, `ResponseGroundedness`, `ContextPrecisionWithoutReference`, `AnswerRelevancy` needs embeddings and is not supported | Ragas |

- **Gate**: each metric's average must reach its minimum; an unknown name fails the build (typo guard). A metric with
  nothing to score (no rule matched, no tool output) reports "no score produced" and fails, so a set that gates
  `rule_followed` must include a case where a rule fires.
- **Calibration**: the report lists gated metrics with no `calibration` entry under `uncalibrated`; their scores
  still gate, but treat a failure as a prompt to look, not proof (see below).
- **Speed**: up to three cases run at once, each on a runtime of its own; judged scores run one at a time to stay
  inside provider rate limits.
- **Judge**: `EVALS_JUDGE_MODEL`, any litellm model id, so evals need no Anthropic credit. Without it only the
  expectations run.
- **Simulate first**: the console's **Simulate** tab and `yachat simulate` run the same kinds of conversations
  without expectations or scores, to explore an agent before you write its set.

## Calibration (required before a metric gates)

Label ~30 conversations per agent by hand (pass/fail per metric), run the judge, record agreement in
`thresholds.yaml: calibration` and here:

| Agent | Metric | Human-labeled set | Agreement | Date |
|---|---|---|---|---|
| hiring-agent | (none yet) | | | |

Until a row exists for a metric, treat its score as advisory, not a gate.

## Online (production)

- Nightly job: `yachat.evals.online.nightly_sample` redacts live sessions (PII patterns; field values never leave), then scores
  the reference-free subset (`yachat.evals.metrics.online_metrics`).
- Tracked per agent, policy version, channel, language, step: completion rate, wrong-exit rate, re-asks per field, p95
  latency (`/v1/metrics`), cost per conversation (from `llm_calls` and model prices).
- Outcome split (`yachat.evals.online.OutcomeSplit`, outcome = application submitted): digressions, Intent Detector failures per
  field, language switches, all-steps-complete, early exits by reason. Summary: digression lift, drop-off after a re-ask
  failure per field, wrong exits (disengaged, later returned and submitted). Correlations only; confirm with A/B tests
  on policy versions.
- Export a failed conversation as a replay test: `uv run yachat evals export <session_id> --state-file state.json [--out evals/hiring_agent/replay.yaml]`.

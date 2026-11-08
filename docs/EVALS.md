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
  `nudges`, `reasks`, `language`, `rules`, `rules_fired` and `rules_not_fired` (on any turn). For `steps`, `fields` and
  `reasks`, only the keys you give are checked. Unknown keys are rejected. `note` says why a case exists.
- Persona cases vary between runs: keep their expectations to outcomes (`exit_reason`), not exact fields.

### Checks every conversation must pass

Besides its own `expect`, every case is held to checks that come from the policy (`yachat/evals/checks.py`), so a
conversation needs no expected answers to be a test:

| Check | Fails when | How |
|---|---|---|
| `no_errors` | a stage failed and the reply fell back | code |
| `no_empty_reply` | the agent sent nothing | code |
| `no_loop` | the same reply three turns in a row | code |
| `within_limits` | re-asks, digressions or nudges went past the policy's limits | code |
| `language_allowed` | the conversation ended in a language the policy does not list | code |
| `in_scope` | a reply does work the charter puts out of scope; the judge must quote the sentence, and a quote not in the reply does not count | judge |
| `rule_followed:<rule>` | on a turn where the rule matched, the reply leaves out a part of its `then` that applies; each part done needs a quote from the reply (earlier turns count as known) | judge |

`yachat evals run` applies the code checks. `yachat evals check` applies the judged ones too (when
`EVALS_JUDGE_MODEL` is set) instead of scoring the thresholds' metrics. Generated cases kept for an `in_scope` or
`rule_followed` failure carry no `expect`, so only `evals check` catches them again: run both in CI.

```bash
uv run yachat evals check evals/support_assistant
```

### Generating a set

```bash
uv run yachat evals generate examples/support_assistant.yaml           # writes evals/support_assistant/generated.yaml
uv run yachat evals generate examples/support_assistant.yaml --only rule: handed_off
```

1. **Targets come from the policy, in code**: finishing the flow, giving up, a bad answer to each form field, each
   rule and a near miss of it, an out-of-scope request, asking for a person (when `handoff.when` has
   `user_request`), each extra language, and for agents without a flow, ordinary questions that need its tools.
2. **The model plays a user aiming at each target**, on the simulator, with the set's `fixtures`. It writes only what
   the user says; what a case must achieve (`expect`) comes from the target.
3. **Every conversation goes through the checks.** One that fails is shortened: messages are dropped while the replayed
   script still fails, so it becomes a short script. One that reaches its target is replayed once as a script; it
   keeps its `expect` only if the script reaches the target on its own and passes the checks. One that misses it is
   tried again, then reported with the reason (the played user may not have done it, the agent did not respond to
   it, or the provider failed).
4. **Output**: `generated.yaml` next to `replay.yaml`. A run replaces the cases of the targets it played and keeps
   the rest (so `--only` adds to the file), so review it as a diff and commit what you keep. `evals run` and
   `evals check` read both files; a case name used in both is an error. A `thresholds.yaml` naming the policy is written if the folder
   has none. The command exits 1 when a generated case fails.

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
trials: 3                          # runs per case (default 1); a case passes only when every run does
calibration: {in_scope: 0.95, rule_followed: 0.95}  # from `yachat evals calibrate`
```

Metric names:

| Name | Scored on | Library |
|---|---|---|
| `conversation_completeness`, `knowledge_retention`, `role_adherence`, `conversation_relevancy` | each conversation | DeepEval |
| `faithfulness`, `task_completion`, `pii_leakage`, `non_advice`, `role_violation`, `tool_correctness`, `argument_correctness` | each agent turn, with that turn's tool output as context | DeepEval |
| a name under `rubrics:` | each conversation (ConversationalGEval with your criteria) | DeepEval |
| `rule_followed` | each turn where a policy rule matched: the judge splits the rule's `then` into parts and quotes where the reply does each; code passes it only when every part that applies is done | judge (yachat) |
| `ragas.<Metric>` | each turn with output from a `context_tools` tool: `user_input`, `response`, `retrieved_contexts`. Reference-free ones work: `Faithfulness`, `ResponseGroundedness`, `ContextPrecisionWithoutReference`, `AnswerRelevancy` needs embeddings and is not supported | Ragas |

- **Gate**: each metric's average must reach its minimum; an unknown name fails the build (typo guard). A metric with
  nothing to score (no rule matched, no tool output) reports "no score produced" and fails, so a set that gates
  `rule_followed` must include a case where a rule fires.
- **Calibration**: the report lists gated metrics with no `calibration` entry under `uncalibrated`; their scores
  still gate, but treat a failure as a prompt to look, not proof (see [Calibration](#calibration)).
- **Trials**: the model answers differently each run, so one pass proves little. With `trials: k` (or `--trials k`
  on `evals run` and `evals check`) each case runs k times and passes only when every run does (pass^k). The report
  gives `passed_every_trial`, and `flaky` lists cases that passed some runs but not all; each failure says which
  trial it came from. Metrics average over every run. Cost and time grow k times.
- **Speed**: up to three cases run at once, each on a runtime of its own; judged scores run one at a time to stay
  inside provider rate limits.
- **Judge**: `EVALS_JUDGE_MODEL`, any litellm model id, so evals need no Anthropic credit. Without it only the
  expectations run.
- **Simulate first**: the console's **Simulate** tab and `yachat simulate` run the same kinds of conversations
  without expectations or scores, to explore an agent before you write its set.

## Calibration

A judge can be wrong. `in_scope` and `rule_followed` failures fail `evals check` either way; calibration tells you how
far to trust them. Measure how often the judge agrees with a person:

1. In `evals/<agent>/labels.yaml`, write replies and mark each one pass or fail by hand. Aim for 20+ per check,
   about half of them failing, close calls included. The last turn is the reply judged; earlier turns are context.

   ```yaml
   labels:
     - check: rule_followed:damaged_item       # or in_scope
       turns:
         - {user: "my order number is ORD-12345", agent: "Thanks! What went wrong?"}
         - {user: "the lamp arrived cracked", agent: "Sorry! We'll send a free replacement for ORD-12345."}
       pass: true
       note: order number already known       # optional: why
   ```

2. Run `EVALS_JUDGE_MODEL=... uv run yachat evals calibrate evals/<agent>`. It runs the same judged checks on every
   label and prints the agreement per check, how often the judge was too lenient or too strict, and each
   disagreement with the judge's reason. It exits 1 when a check agrees less than 80% or a label could not be judged.
3. Copy the printed `calibration:` block into `thresholds.yaml`. The judge varies too: run it twice and record the
   lower figure. Rerun it when you change the judge model or the prompts in `yachat/evals/checks.py`.

A disagreement means either the judge or the label is wrong: fix the label, or check that check's failures by hand
until the judge improves. Metrics with no `calibration` entry are listed under `uncalibrated` in the report.

Labels written together with the judge's prompt flatter it. The support labels below were, so treat their figures as
a ceiling until labels from real conversations are added.

| Agent | Check | Labels | Agreement | Judge | Date |
|---|---|---|---|---|---|
| support-assistant | `in_scope` | 20 | 95–100% over two runs | Qwen3.5-27B | 2026-11-05 |
| support-assistant | `rule_followed` | 20 | 95–100% over two runs (90–95% before the rule was judged part by part) | Qwen3.5-27B | 2026-11-05 |

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

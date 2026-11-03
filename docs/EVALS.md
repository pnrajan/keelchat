# Evaluation

Harness: `evals/` (platform-owned). Per agent: `evals/<agent>/replay.yaml`, `personas.yaml`, `thresholds.yaml` (team-owned).

## Offline (CI gate)

```bash
uv run yachat evals run                 # every agent dir with thresholds.yaml; non-zero exit on failure
uv run yachat evals run evals/hiring_agent
EVALS_JUDGE_MODEL=openrouter/openai/gpt-4o-mini uv run yachat evals run   # with DeepEval LLM-judged metrics
```

- **Replay set**: every hard case in the brief's section 10 as a scripted conversation (`ReplayCase`) run against the
  runtime with fake tools and the configured real model. Each case carries structural expectations (exit reason, fields,
  digressions, tools called) that are checked without a judge.
- **Simulated users**: personas (`evals/personas.py`): Spanish with typos, SMS shorthand, chatty non-serious, returning
  candidate. Scripted offline; `simulate_with_deepeval` wires DeepEval's `ConversationSimulator` when a judge is set.
- **Metrics** (`evals/metrics.py`, DeepEval names): Conversation Completeness, Knowledge Retention, Role Adherence,
  Conversation Relevancy (`ConversationalTestCase`); Faithfulness per agent turn with that turn's tool outputs as
  `retrieval_context`; Tool/Argument Correctness (offline only); Task Completion (planner turns); PIILeakage, Non-Advice,
  Role Violation; `ConversationalGEval` rubrics: no language switch on names, confirm before switching, job text as posted.
- **Gate**: `thresholds.yaml` minimums; any drop fails the build (`evals.metrics.gate`).
- **Judge**: `evals/judge.py` runs DeepEval metrics over litellm (SiliconFlow, OpenRouter, OpenAI) so no Anthropic credit is needed.

## Calibration (required before a metric gates)

Label ~30 conversations per agent by hand (pass/fail per metric), run the judge, record agreement in
`thresholds.yaml: calibration` and here:

| Agent | Metric | Human-labeled set | Agreement | Date |
|---|---|---|---|---|
| hiring-agent | (none yet) | | | |

Until a row exists for a metric, treat its score as advisory, not a gate.

## Online (production)

- Nightly job: `evals.online.nightly_sample` redacts live sessions (PII patterns; field values never leave), then scores
  the reference-free subset (`evals.metrics.online_metrics`).
- Tracked per agent, policy version, channel, language, step: completion rate, wrong-exit rate, re-asks per field, p95
  latency (`/v1/metrics`), cost per conversation (from `llm_calls` and model prices).
- Outcome split (`evals.online.OutcomeSplit`, outcome = application submitted): digressions, Intent Detector failures per
  field, language switches, all-steps-complete, early exits by reason. Summary: digression lift, drop-off after a re-ask
  failure per field, wrong exits (disengaged, later returned and submitted). Correlations only; confirm with A/B tests
  on policy versions.
- Export a failed conversation as a replay test: `uv run yachat evals export <session_id> --state-file state.json [--out evals/hiring_agent/replay.yaml]`.

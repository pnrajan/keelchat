# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- `yachat evals run` in the package (`yachat[evals]`): replay sets on the simulator, per-turn rule checks, `rule_followed`, Ragas metrics, eval-set rubrics.
- `yachat evals generate`: an eval set from the policy, one case per target, failures shortened to short scripts.
- Console **Review** tab: people rate conversations (per reply and overall); ratings export as `labels.yaml` and replay cases.
- `--trials k` / `trials:` on evals: each case runs k times and passes only when every run does; flaky cases listed.
- `rule_followed` judged part by part: each part of the rule that applies needs a quote from the reply; code decides.
- `yachat evals calibrate`: the judge's agreement with replies a person labeled (`labels.yaml`).
- `yachat evals check` and checks every conversation must pass (no errors, loops or broken limits; in scope; rules followed).
- Handoffs resume: per-ticket `callback_url`, **Resolve and resume**, `handoff.timeout`.
- `rules:` on agents and flow steps.
- `model: small | large` and `high_stakes` per step.
- `from yachat import ...` extension API; `py.typed`.
- `yachat new agent|package`.
- `pytest --offline`.
- `@metered` for LLM token and cost recording.
- LiteLLM gateway cost; Headroom saved tokens.
- Analytics Service model calls count toward the turn.
- Dashboards add up in DuckDB (now a core dependency).

### Changed
- Eval sets: `thresholds.yaml` takes `policy:` instead of `agent:`; import from `yachat.evals`.

### Fixed
- `./run.sh` keeps extras from `uv sync --all-extras`.
- Booking example guardrail typed with `GuardContext`.
- Planner no longer offers a `dataset` argument to agents without datasets.
- Judged evals run end to end: DeepEval 4 metric names, `options:` for metric arguments, charter as chatbot role.

## [0.1.0]

### Added
- First open-source release: YAML-defined chatbots on one shared runtime, with capability registry, channels, stores,
  Knowledge and Analytics services, simulator, evals and a test console.

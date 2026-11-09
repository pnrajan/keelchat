# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- `./setup.sh`: checks for git, curl, uv, Node 20+ (and Docker with `--docker`) on macOS, Debian/Ubuntu, Fedora, Arch
  and WSL, offers to install what's missing, writes `.env` with your model key and installs the dependencies.
- `scripts/local.sh up|logs|down|reset`: the whole stack in Docker (API with the console, Redis, Postgres), on the next
  free ports, with data kept in volumes.
- `closing` on `form_fill`: what to say once every field is answered, e.g. what happens next. The support example sets it.
- `resume` on `POST /v1/sessions` (default true). The console's **New session** always starts afresh.
- `yachat evals run` in the package (`yachat[evals]`): replay sets on the simulator, per-turn rule checks, `rule_followed`, Ragas metrics, eval-set rubrics.
- `yachat evals generate`: an eval set from the policy, one case per target, failures shortened to short scripts.
- Console **Rate** tab: people rate conversations (per reply and overall); ratings export as `labels.yaml` and replay cases.
- Console **Tests** tab: tests kept with the agent (in the store); save a conversation as a test, draft tests from the policy, run them on the current build, export as `replay.yaml`. The launch checklist counts them.
- [The policy language](docs/POLICY_LANGUAGE.md): every key, value and default in one page; a test keeps it in step with the build. Linked from Compose's Show YAML.
- Console **Judge** tab: a stronger model (`YACHAT_MODEL_CRITIC`) judges a trial conversation; policy fixes applied
  in the editor, code fixes kept as fix tasks (`/v1/tenants/{t}/fixes`). MCP tools `list_fix_tasks`, `get_fix_task`,
  `update_fix_task`, and the plugin's **fix-findings** skill: GitHub issues, then one subagent per fix with a failing
  test first.
- Console Setup shows only what needs work; passed checks, step details and rarely used settings are folded.
- Console Inspector in four groups (Setup, This reply, Test, More); stage timings folded.
- Console **Compose** view: say what the bot should do, read what it will do in plain words (`/v1/agents/validate` now returns this summary), change it in plain words (`POST /v1/agents/edit`, checked by the build, listed before applying), then **Try it**. Fine-tune cards and the YAML are folded below.
- The drafter writes `rules`, `handoff`, `small_talk`, `think` and `curious` when a description asks for them.
- `think: true` on a step: the reply is planned first (need, what is unknown, the rules it must follow); code adds any matched rule the plan missed; the plan shows in the Why tab.
- `small_talk: off | brief | warm` on a policy: chit-chat answered in kind, then back to the question; not counted and never read as an answer, up to two turns in a row (counted in code).
- `curious: true` on form steps: one follow-up when a valid answer is too thin to act on, counted in code (one per field); drafted tests cover it.
- **Suggest policy changes** from ratings: the model revises the policy to answer failed replies and low scores, each change named; checked by the build, never saved.
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
- Support example accepts order numbers in any format (`#98765` as well as `ORD-12345`).
- Eval sets: `thresholds.yaml` takes `policy:` instead of `agent:`; import from `yachat.evals`.

### Fixed

- The Docker image sets `HOME` for the unprivileged user, so connecting to Postgres no longer fails on
  `/root/.postgresql` permissions.
- The plain-words summary reads each rule as one sentence: "When the user describes ...", not "When The user ...".
- A session is resumed only on the build it was asked for: a different `policy_hash` starts a new conversation instead of picking up an old one on another build.
- When the documents don't answer a question and the policy hands off on request, the reply says the user can ask for a person.
- A handoff trigger listed twice is rejected with its path (`handoff.when`).
- Answers from documents that do not answer the question say so, instead of describing the documents.
- After a person resolves a handoff, the user's next message continues the flow instead of counting as a digression.
- `./run.sh` keeps extras from `uv sync --all-extras`.
- Booking example guardrail typed with `GuardContext`.
- Planner no longer offers a `dataset` argument to agents without datasets.
- Judged evals run end to end: DeepEval 4 metric names, `options:` for metric arguments, charter as chatbot role.

## [0.1.0]

### Added
- First open-source release: YAML-defined chatbots on one shared runtime, with capability registry, channels, stores,
  Knowledge and Analytics services, simulator, evals and a test console.

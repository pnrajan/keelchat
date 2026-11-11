# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Releases publish to PyPI from GitHub with Trusted Publishing (`.github/workflows/release.yml`); steps in
  CONTRIBUTING.md.

## [0.2.0] - 2026-11-11

### Added

- **Compose:** describe a bot in plain English, read back what it will do, change it in plain words, try it.
- **Rules:** `rules:` on agents and steps; `handoff: true` passes the chat to a person in code.
- **Steps:** `think: true` (plan each reply), `curious: true` (one follow-up on thin answers), `closing` on
  `form_fill`, `small_talk`, `model: small | large`, `high_stakes`.
- **Testing:** console Tests, Rate and Judge tabs; `keelchat evals generate | run | check | calibrate`; `--trials`;
  suggested policy fixes from ratings.
- **Handoffs** resume after a person resolves them (`callback_url`, `handoff.timeout`).
- **Deploy** to your own Render (Deploy button) or Fly.io (GitHub workflow); `./setup.sh` and `scripts/local.sh` for
  local runs.
- **Extending:** `from keelchat import ...`, `keelchat new agent | package`, `py.typed`.
- **Costs:** token and cost per model call (`@metered`), LiteLLM gateway and Headroom savings, DuckDB dashboards.
- [The policy language](docs/POLICY_LANGUAGE.md): every key on one page.

### Changed

- Settings are `KEELCHAT_*`; Redis sessions use the prefix `keelchat:`.
- Eval sets: `thresholds.yaml` takes `policy:`; import from `keelchat.evals`.

### Fixed

- Hidden model reasoning no longer uses up the token budget (OpenRouter, SiliconFlow).
- A session resumes only on the build it started on.
- Answers from documents that don't cover the question say so.
- Docker image: Postgres connections work as the unprivileged user.

## [0.1.0]

### Added

- First open-source release: YAML-defined chatbots on one shared runtime, with a capability registry, channels,
  stores, Knowledge and Analytics services, a simulator, evals and a test console.

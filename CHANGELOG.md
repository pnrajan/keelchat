# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Knowledge search parts are replaceable: embedding model (`@embedder`: any provider through litellm,
  sentence-transformers locally), passage splitting (`@chunker`), indexing method (`@indexer`, also usable as a
  knowledge base's `strategy`), and vector store (`@vector_store`). Each is picked by a setting
  (`KNOWLEDGE_EMBEDDER`, `KNOWLEDGE_CHUNKER`, `KNOWLEDGE_STRATEGY`, `KNOWLEDGE_VECTOR_STORE`); `GET /v1/registry`
  lists them. Each embedder gets its own index table.
- **Knowledge bases** in Compose: upload files and web pages, or pick one you've made; the bot change is proposed to
  apply. `POST /v1/tenants/{t}/documents`.
- Each knowledge base picks its indexing method from its documents (full context, contextual retrieval, RAPTOR, or
  search) and says why; `strategy` and `top_k` override.
- Without `KNOWLEDGE_URL`, the API indexes and searches knowledge bases itself, so Render, Fly.io and `scripts/try.sh`
  answer from documents with nothing else to run. The Docker image sets `KEELCHAT_DATA_ROOT=/data`.
- Releases publish to PyPI from GitHub with Trusted Publishing (`.github/workflows/release.yml`); steps in
  CONTRIBUTING.md.

### Fixed

- A nested object sent by the model as a JSON string is read as the object; a plan cut off mid-way is asked for again.
- The launch checklist no longer calls a bot's knowledge base sample data.
- Dataset responses include `kind`, which the console's YAML editor reads.

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

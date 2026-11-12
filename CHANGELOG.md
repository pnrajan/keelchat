# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- [Building agents with keelchat: a plain guide](docs/guide/building-agents.pdf) (PDF, LaTeX source beside it): diagrams, six
  recipes (support, booking, survey, documents, data, your own tool), testing, extending and going live. Every YAML
  example builds (`tests/test_guide.py`).
- Demo: while it asks questions or drafts, a generic working animation, like chat apps: a spinner, a shimmering
  "Thinking…" line, the seconds so far, and placeholder lines drawing like a document being written; still when the
  visitor prefers reduced motion.
- Demo: before the first draft, the drafting model reads the description against what a policy can express, says
  what it understood, and asks 2-5 questions it judges matter most for that bot (no fixed topics), each with a line of
  guidance and options to click (or your own words); the answers go into the draft. Drafting uses Claude Sonnet 5.5
  through OpenRouter. `keelchat.policy.drafter.clarify` and `with_answers`.
- chonkie splits documents into passages (semantic by default) and provides a small local embedding model, used when
  no API embedding model is set; included in the `api` and `knowledge` extras and the Docker image.
- Vector stores for Qdrant, Chroma and pgvector (`KNOWLEDGE_VECTOR_STORE`), and `keelchat.testing.check_vector_store`
  to test any store, your own included.
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

### Changed

- Docs checked against the code and corrected throughout (policy defaults, guardrail defaults, memory types,
  architecture, knowledge search, the API walkthrough, extension examples). A plugin README; complete policies in the
  docs are built by `tests/test_docs.py`.
- README and docs screenshots re-taken from the current console and demo: Compose shows the Knowledge base panel; a new
  image shows the demo's questions before drafting.

### Fixed

- `@tool(..., side_effects="write")` is no longer retried by default (a retried write could happen twice), as HTTP
  and MCP tools already weren't; an explicit `retries` is kept.
- `KEELCHAT_LLM=litellm` works with any provider litellm supports, using its usual key variables (e.g.
  `GEMINI_API_KEY`); a missing one is named at start. `run.sh` no longer limits it to three providers.
- `keelchat new package`: the generated tests pass when run from the package folder (no pytest plugin needed; the
  model key is read from a `.env` there).
- A bad memory item's error names its position (`memory[1].type`).
- The MCP server's instructions no longer name a tool that doesn't exist.
- `deploy/fly.toml` sets `KEELCHAT_CLIENT_IP_HEADER`, so the public widget's per-visitor limits see each visitor.
- pgvector finds a small knowledge base beside a large one (iterative index scans), and takes embeddings over 2000
  dimensions; tables are created by one process at a time.
- Qdrant and Chroma refuse an `s3://` index address instead of writing to a local folder named after it.
- A re-index that failed part-way embeds the documents still missing; a changed method is applied after a restart.
- An embedding model litellm can't route (an older `.env`'s SiliconFlow model name, no key) no longer breaks indexing;
  embedding failures return 502 with a short reason.
- A vector store, chunker or embedder you register under a built-in name is kept.
- A vector store, chunker or embedder you register under a built-in name is kept.
- Tests that build the knowledge service no longer read `KNOWLEDGE_S3_BUCKET` from `.env`, so they never write to a
  real bucket. A local Qdrant no longer warns about filter indexes.
- A knowledge base whose documents were kept but whose vectors were missing (a new vector store or embedding model) is
  embedded again instead of being skipped as unchanged.
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

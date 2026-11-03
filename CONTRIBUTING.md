# Contributing

Thanks for helping. yachat builds goal-oriented chatbots from YAML; most contributions are one
of: a new capability (tool, node, guardrail, pre-processor, action), a runtime fix, an example agent, or an eval case.

## Setup

```bash
git clone https://github.com/pnrajan/yachat && cd yachat
cp .env.example .env            # a model provider key is required even for tests
uv sync --all-extras
uv run pre-commit install       # ruff + mypy on commit
./run.sh                        # console, API, Knowledge and Analytics Services
```

## Before opening a pull request

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                   # calls the model in .env; 3 workers; one automatic rerun per flaky test
uv run pytest --offline         # no key: only the tests that don't call the model (CI runs this on forks)
cd frontend && npm run build    # when the console changed
uv run yachat evals run   # when a policy, prompt or example agent changed
```

- Tests call the real model. Keep new tests behavioural (what the runtime asked, what the state became), not tied to
  exact model wording. `tests/helpers.py:RecordingLLM` records every request.
- A capability must pass the contract suite (`tests/test_registry.py`). See `docs/ADDING_A_CAPABILITY.md`.
- A behaviour change to a flow needs a replay case in `evals/<agent>/replay.yaml`.
- Scope and ambiguity: follow `CLAUDE.md`; pick the simplest option and say so in the PR.

## Pull requests

- One topic per PR. Explain the user-visible behaviour change in the description and link the eval or test that
  proves it.
- Never commit `.env`, keys, or real conversation data. Replay cases must use synthetic names and addresses.
- Prompts live next to the code that sends them (`intent_examples.yaml`, `planner.py:verify_system`); changes to
  them are reviewed like code and need a replay case.

## Reporting problems

Bugs and feature requests: GitHub issues (templates provided). Security issues: see `SECURITY.md`, do not open a
public issue.

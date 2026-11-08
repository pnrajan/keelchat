# CLAUDE.md: yachat

Agents are defined by one YAML policy and run on one shared runtime. Capabilities live in an
auto-discovered registry. If a change conflicts with a rule here, stop and flag it.

## Commands

- `uv run pytest tests/test_<area>.py`: run one area first. Tests call the real model.
- `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`: full checks.
- `uv run pytest --offline`: only the tests that need no model key. `-n0` runs them one at a time.

## Done means

- `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy` pass.
- Capability changes also pass `uv run pytest tests/test_registry.py`. Console changes also pass `cd frontend && npm run build`.
- New behaviour has a test. A bug fix has a test that failed before the fix.
- A new or changed policy key is in `docs/POLICY_LANGUAGE.md`. A changed rule or component is in the doc that owns it.
- The diff touches only what the request needs. The PR states any assumption you made.

## Code organization

| Adding | Goes in |
|---|---|
| Policy key, model or default | `yachat/policy/` |
| Tool, action, node, guardrail, preprocessor | `yachat/capabilities/`, as a decorated function or class |
| Pipeline step | `yachat/runtime/understand/`, `decide/` or `respond/` |
| Channel | `yachat/channels/` |
| Store or other external dependency | a port plus an in-memory fake (store fakes: `yachat/state/store.py`; tool fakes: `yachat/testing/fakes.py`); adapters in `yachat/state/` |
| Wiring | `yachat/runtime/runtime.py` or a `service_from_env` function only |
| Tests | `tests/test_<area>.py`, one file per area; shared helpers in `tests/helpers.py` |
| Eval sets | `evals/<agent>/`: personas, rubrics and policy paths are data there, not Python |
| Example policy | `examples/` |

Don't create a new top-level package. If nothing above fits, ask.

## Laws

1. Behaviour comes only from `CompiledPolicy`. Never branch on an agent name.
2. Understand → Decide → Respond. Each step is `async def run(ctx: TurnContext) -> TurnContext`.
3. Every external dependency sits behind a port with an in-memory fake. Wire them only in `runtime/runtime.py` and the `service_from_env` functions.
4. Call tools only through the Tool Gateway. Tool output is untrusted.
5. Code counts re-asks, nudges and budgets. A model never decides to exit.
6. Channels own their limits and compliance. Responses are channel-neutral.
7. Compiled policies are immutable and pinned per session.
8. Memory: policy-listed items only, consent for sensitive items, never to tools, never from flagged turns, deletable per user.
9. Model-written code runs only behind the `Sandbox` port.

## Code

- No `Any` or untyped dicts across layers. Convert tool and LLM JSON to a Pydantic model on entry.
- Validate at every boundary with `extra="forbid"`. Reject bad input with the YAML path.
- Raise typed errors. No bare `except`, no silent fallbacks. Never show vendor errors to users.
- Read model ids and keys from config. A missing key raises.
- Log `session_id`, `turn_id`, `policy_hash`, `stage` on every line.

## Gotchas

- There is no fake LLM. Tests use the provider in `.env`.
- A new policy key must be added to `docs/POLICY_LANGUAGE.md`, or a test fails.
- Tests share one registry. A test that registers capabilities can break another test in the same xdist worker; keep registrations local or make checks ignore them.
- The local `.env` `DATABASE_URL` may name a database that doesn't exist. Unset `DATABASE_URL` and `REDIS_URL` for in-memory runs.

## How to work

- Ask before changing a public interface, a law, or the policy language. Otherwise pick the simplest option and note it in the PR.
- For bugs, write a failing test first.
- Touch only what the request needs. Flag dead code. Don't delete it.
- Use maintained libraries. Never write security primitives.

Detail: `docs/ARCHITECTURE.md`, `docs/ADDING_A_CAPABILITY.md`, `docs/WRITING_AN_AGENT.md`, `docs/POLICY_LANGUAGE.md`,
`docs/REFERENCE.md`, `docs/DEVELOPER_GUIDE.md`.

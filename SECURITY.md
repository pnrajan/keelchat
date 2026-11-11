# Security

## Reporting a vulnerability

Email pankaj@pankajrajan.com with a description and reproduction steps. Do not open a public issue. You will get an
acknowledgement within three working days.

## What the runtime does to protect users

- **Tool Gateway**: every tool call passes an allow list from the agent policy, timeouts, a circuit breaker and a
  scrub that withholds sensitive memory values (exact and substring). Candidate-only actions (`submit_application`)
  run only from the candidate's own explicit action, never from a node or the planner.
- **Untrusted content**: tool and document output is fenced as data in prompts (`<tool_output trusted="false">`,
  with `<` escaped) and never treated as instructions. Input guardrails (`prompt_injection` always on) run before
  the model sees a message; output guardrails (`pii` always on) run before a reply leaves.
- **Memory**: only policy-listed fields are extracted; sensitive fields need consent and must be volunteered;
  flagged turns are never used; values decay by half-life; `DELETE /v1/users/{id}/memory` removes everything.
- **Datasets and analytics**: dataset sources must sit under `KEELCHAT_DATA_ROOT` or an allowed bucket (checked
  with symlinks resolved); a policy reaches only the datasets its steps name, in its own tenant. SQL runs on an
  in-memory DuckDB copy with file, network and extension access disabled and settings locked: one SELECT, row cap,
  timeout. Model-written Python runs only in a sandbox (never in the service process), a fresh one per run, with no
  network, CPU, memory and file limits, and read-only system paths; query results are untrusted data in prompts.
- **Logs**: structured JSON with ids, never secrets; user text is PII-redacted before logging.
- **API auth**: `KEELCHAT_API_KEY` gates every route of both services when set. Without it the APIs accept any
  caller; do not expose them publicly without it and a reverse proxy.

## Known limitations

Tool Registry API registrations, including any headers a tenant supplies, are stored in the `tenant_tools` table;
restrict access to the database accordingly.

Open items, not yet scheduled: `user_id` is caller-supplied until an identity provider is wired, and non-sensitive
memory items are placed in the model's system prompt on every turn (planner, knowledge node and response generator
alike); only sensitive items are withheld. Report anything that turns these into an exploit as a vulnerability.

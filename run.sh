#!/usr/bin/env bash
# One command to run keelchat locally: checks prerequisites, then starts console + API + Knowledge + Analytics services.
#
#   ./run.sh                 # same as scripts/dev.sh --knowledge --analytics (model from .env, sample docs and data)
#   ./run.sh --sandbox       # also run analytics statistics code in a local Docker sandbox
#   ./run.sh --build         # single port: http://127.0.0.1:8000/
#   ./run.sh --fake-tools    # API and console only: no Knowledge or Analytics Service; FakeTools for every tool
#   ./run.sh -- <dev.sh flags>
set -euo pipefail
cd "$(dirname "$0")"

for tool in uv node npm; do command -v "$tool" >/dev/null || { echo "missing: $tool (see README Quickstart)" >&2; exit 1; }; done
if [ ! -f .env ]; then
  cp .env.example .env
  echo "created .env from .env.example; set KEELCHAT_LLM and the provider key, then rerun" >&2
  exit 1
fi
set -a; . ./.env; set +a
case "${KEELCHAT_LLM:-}" in
  anthropic) [ -n "${ANTHROPIC_API_KEY:-}" ] || { echo "ANTHROPIC_API_KEY is empty in .env" >&2; exit 1; } ;;
  litellm) ;;  # any provider litellm supports: the API names a missing key for KEELCHAT_MODEL_GENERATE when it starts
  *) echo "set KEELCHAT_LLM=anthropic|litellm in .env" >&2; exit 1 ;;
esac

ARGS=(--knowledge --analytics)
case "${1:-}" in
  --fake-tools) ARGS=(--fake-tools); shift ;;
  --sandbox) ARGS+=(--sandbox); shift ;;
  --) shift; ARGS=() ;;
esac
exec scripts/dev.sh ${ARGS[@]+"${ARGS[@]}"} "$@"   # (bash 3.2: an empty array needs this form under set -u)

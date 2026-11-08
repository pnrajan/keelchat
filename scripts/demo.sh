#!/usr/bin/env bash
# Deploy the public demo (examples/demo/app.py) to Fly.io with the current checkout. deploy/demo/fly.toml has the setup.
#
#   scripts/demo.sh             # build and deploy, then check the live site
#   scripts/demo.sh --secrets   # first copy the model settings and DEMO_KEYS from .env to Fly (values never printed)
#   DEMO_KEYS="ANTHROPIC_API_KEY" scripts/demo.sh --secrets   # the provider keys the demo's models need
set -euo pipefail
cd "$(dirname "$0")/.."
CONFIG=deploy/demo/fly.toml
KEYS=${DEMO_KEYS:-SILICONFLOW_API_KEY}  # only these provider keys go to Fly, not every key in .env
URL=https://yachat-demo.fly.dev

command -v fly >/dev/null || { echo "missing: fly (https://fly.io/docs/flyctl/install/)" >&2; exit 1; }
if [ "${1:-}" = "--secrets" ]; then
  [ -f .env ] || { echo "no .env" >&2; exit 1; }
  # shellcheck disable=SC2046
  fly secrets set -c "$CONFIG" --stage $(grep -E "^(YACHAT_LLM|YACHAT_MODEL_GENERATE|YACHAT_MODEL_CLASSIFY|${KEYS// /|})=" .env | xargs) >/dev/null
  fly secrets list -c "$CONFIG" --json | python3 -c 'import json,sys; print("secrets:", ", ".join(s["Name"] for s in json.load(sys.stdin)))'
fi
# the console and API stay locked: set a random key once, never print it
fly secrets list -c "$CONFIG" --json | grep -q '"YACHAT_API_KEY"' || fly secrets set -c "$CONFIG" --stage YACHAT_API_KEY="$(openssl rand -hex 24)" >/dev/null

fly deploy -c "$CONFIG" --ha=false .

curl -sf "$URL/health" >/dev/null || { echo "the demo is not answering at $URL/health" >&2; exit 1; }
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$URL/v1/public/embeds/pk_demo_support/sessions" -H 'content-type: application/json' -d '{}')
[ "$code" = 201 ] || { echo "the demo chat did not start (HTTP $code)" >&2; exit 1; }
echo "demo is live: $URL"

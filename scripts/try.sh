#!/usr/bin/env bash
# Try the console with a sample agent and tests, without Postgres or Redis: everything in memory, gone on exit.
#
#   scripts/try.sh                # http://127.0.0.1:8000/console/ (the next free port if 8000 is taken)
#   scripts/try.sh --port 8010
#   scripts/try.sh --judge        # also run the judged checks (EVALS_JUDGE_MODEL from .env); each test takes minutes
#   scripts/try.sh --no-open      # do not open the browser
#
# Saves examples/support_assistant.yaml as the agent `support-assistant` with three tests and opens the console. Pick it
# under Saved agents, press Build & try, then open the Test tab and press Run.
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=8000 JUDGE='' JUDGED=off OPEN=1
while [ $# -gt 0 ]; do
  case "$1" in
    --port) [ $# -ge 2 ] || { echo "--port needs a number" >&2; exit 2; }; PORT=$2; shift 2 ;;
    --judge) JUDGE=1 JUDGED=on; shift ;;
    --no-open) OPEN=''; shift ;;
    -h|--help) sed -n 2,10p "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done
for tool in uv npm curl; do command -v "$tool" >/dev/null || { echo "missing: $tool (see README Quickstart)" >&2; exit 1; }; done
[ -f .env ] || { echo "no .env: cp .env.example .env and set KEELCHAT_LLM and its key" >&2; exit 1; }
while curl -s -o /dev/null "http://127.0.0.1:$PORT/"; do PORT=$((PORT + 1)); done  # skip ports in use

# empty values win over .env (the app reads it too): in-memory stores, no API key, sample tools, judge off unless asked
export DATABASE_URL='' REDIS_URL='' KEELCHAT_API_KEY=''
export KEELCHAT_TOOLS=fake  # tools without a backend URL answer from built-in sample data (knowledge, jobs)
export KEELCHAT_DATA_ROOT="${KEELCHAT_DATA_ROOT:-$PWD/.data}"  # uploaded documents
[ -n "$JUDGE" ] || export EVALS_JUDGE_MODEL=''

uv sync -q --inexact
[ -d frontend/node_modules ] || (cd frontend && npm install --silent)
(cd frontend && npm run build --silent)

uv run uvicorn keelchat.api.app:create_app --factory --host 127.0.0.1 --port "$PORT" &
API_PID=$!
trap 'kill $API_PID 2>/dev/null || true' EXIT INT TERM
BASE=http://127.0.0.1:$PORT
for _ in $(seq 1 120); do curl -sf "$BASE/health" >/dev/null && break; sleep 0.5; done
curl -sf "$BASE/health" >/dev/null || { echo "the API did not start" >&2; exit 1; }

post() { curl -sf -X POST "$BASE$1" -H 'content-type: application/json' -d "$2" >/dev/null; }
POLICY=$(uv run python -c 'import json; print(json.dumps({"yaml": open("examples/support_assistant.yaml").read()}))')
AGENT=/v1/tenants/default/agents/support-assistant
saved() {
  post /v1/tenants/default/agents "$POLICY" && post /v1/agents "$POLICY" || return 1
  post "$AGENT/tests" '{"source":"manual","case":{"name":"finishes","messages":["hi, I need help with an order","yes","ORD-12345","it never arrived"],"expect":{"completed":true}}}' || return 1
  post "$AGENT/tests" '{"source":"manual","case":{"name":"any_order_format","messages":["hi","yes","my order is #98765","it never arrived"],"expect":{"completed":true}}}' || return 1
  post "$AGENT/tests" '{"source":"manual","case":{"name":"asks_for_a_person","messages":["hi","yes","let me talk to a human please"],"expect":{"exit_reason":"handed_off"}}}'
}
saved || { echo "could not save the sample agent and tests" >&2; exit 1; }

cat <<EOF

  keelchat is running: $BASE/console/   (API docs: $BASE/docs, judge $JUDGED)

  Try the Test tab
    1. In the Policy dropdown (top left), pick  support-assistant  under "Saved agents".
    2. Press  Build & try.
    3. In the Inspector (right), open the  Test  tab and press  Run 3 tests  (about a minute).
       Each test shows pass or fail; click one to see the conversation.
    4. Press  Draft tests from the policy  for more, and Keep the ones you want.

  Save your own chat as a test
    5. Press  New session  and chat in the middle pane (try order number #98765).
    6. In the Test tab, under "Save a conversation as a test", press  Save as test.

  Rate replies and get policy suggestions
    7. Open the  Review  tab, mark replies Pass or Fail, add a reason, press  Save rating.
    8. Press  Suggest policy changes, then  Open in editor  to see the revised policy.

  Everything is in memory: it is gone when you stop. Ctrl-C stops it.

EOF
if [ -n "$OPEN" ]; then  # the default browser, where there is one
  if command -v open >/dev/null; then open "$BASE/console/"; elif command -v xdg-open >/dev/null; then xdg-open "$BASE/console/" >/dev/null 2>&1 || true; fi
fi
wait $API_PID

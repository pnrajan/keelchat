#!/usr/bin/env bash
# Run the local chat console: API on :8000 (uvicorn, reload) + Vite dev server on :5173 (proxies /v1).
#
#   scripts/dev.sh                    # model per .env (KEELCHAT_LLM + provider key); tools need JOB_SEARCH_URL / KNOWLEDGE_URL
#   scripts/dev.sh --fake-tools       # FakeTools for every tool whose backend URL is unset (the model is always real)
#   scripts/dev.sh --knowledge        # also start the Knowledge Service on :8001 (indexes examples/knowledge) and route
#                                     # the knowledge_search tool to it; implies --fake-tools for the rest
#   scripts/dev.sh --knowledge-url https://knowledge.example.com   # same, against a deployed service
#   scripts/dev.sh --analytics        # also start the Analytics Service on :8002 and register examples/data as the
#                                     # `sales` dataset (examples/sales_analyst.yaml); add --sandbox for statistics in Docker;
#                                     # implies --fake-tools for the rest
#   scripts/dev.sh --build            # build frontend/dist and serve it from the API at http://127.0.0.1:8000/console/ (no Vite)
set -euo pipefail
cd "$(dirname "$0")/.."

MODE=dev KNOWLEDGE= KN_PID= ANALYTICS= AN_PID= AN_FLAGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --fake-tools) export KEELCHAT_TOOLS=fake; shift ;;
    --knowledge) KNOWLEDGE=local; export KEELCHAT_TOOLS=fake; shift ;;
    --knowledge-url) [ $# -ge 2 ] || { echo "--knowledge-url needs a URL" >&2; exit 2; }; KNOWLEDGE=$2; export KEELCHAT_TOOLS=fake; shift 2 ;;
    --analytics) ANALYTICS=1; export KEELCHAT_TOOLS=fake; shift ;;
    --sandbox) AN_FLAGS=(--sandbox); shift ;;
    --build) MODE=build; shift ;;
    -h|--help) sed -n 2,11p "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

[ -f .env ] || echo "note: no .env found; KEELCHAT_LLM and the provider key must be exported" >&2
uv sync -q --inexact   # keep extras installed earlier (uv sync --all-extras); plain sync would remove them
export KEELCHAT_SYNCED=1
[ -d frontend/node_modules ] || (cd frontend && npm install --silent)

cleanup() { [ -z "${API_PID:-}" ] || kill "$API_PID" 2>/dev/null || true; [ -z "${UI_PID:-}" ] || kill "$UI_PID" 2>/dev/null || true; [ -z "$KN_PID" ] || kill "$KN_PID" 2>/dev/null || true; [ -z "$AN_PID" ] || kill "$AN_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if [ "$KNOWLEDGE" = local ]; then
  scripts/knowledge.sh --port 8001 --index examples/knowledge &
  KN_PID=$!
  export KNOWLEDGE_URL=http://127.0.0.1:8001
elif [ -n "$KNOWLEDGE" ]; then
  export KNOWLEDGE_URL=$KNOWLEDGE
fi

if [ -n "$ANALYTICS" ]; then
  scripts/analytics.sh --port 8002 ${AN_FLAGS[@]+"${AN_FLAGS[@]}"} &
  AN_PID=$!
  export ANALYTICS_URL=http://127.0.0.1:8002 KEELCHAT_DATA_ROOT="$PWD/examples/data"
  (  # once the API is up, register the sample dataset for examples/sales_analyst.yaml
    auth=(); [ -z "${KEELCHAT_API_KEY:-}" ] || auth=(-H "Authorization: Bearer $KEELCHAT_API_KEY")
    for _ in $(seq 1 120); do curl -sf http://127.0.0.1:8002/health >/dev/null && curl -sf http://127.0.0.1:8000/health >/dev/null && break; sleep 0.5; done
    curl -sf ${auth[@]+"${auth[@]}"} -X PUT http://127.0.0.1:8000/v1/tenants/default/datasets/sales -H 'content-type: application/json' -d "{
      \"description\": \"Online store orders, January to June 2026\",
      \"spec\": {\"kind\": \"structured\", \"tables\": [
        {\"name\": \"orders\", \"uri\": \"$PWD/examples/data/orders.csv\", \"description\": \"One row per order\", \"columns\": {\"amount\": \"order total in USD\"}},
        {\"name\": \"targets\", \"uri\": \"$PWD/examples/data/targets.parquet\", \"description\": \"Quarterly revenue target per region\"}]}}" >/dev/null \
      && echo "dataset 'sales' registered (examples/sales_analyst.yaml)" || echo "could not register the sample dataset" >&2
  ) &
fi

if [ "$MODE" = build ]; then
  (cd frontend && npm run build --silent)
  echo "console: http://127.0.0.1:8000/console/"
  uv run uvicorn keelchat.api.app:create_app --factory --host 127.0.0.1 --port 8000 &
  API_PID=$!
  wait $API_PID
  exit $?
fi

uv run uvicorn keelchat.api.app:create_app --factory --host 127.0.0.1 --port 8000 --reload &
API_PID=$!
(cd frontend && npm run dev -- --host 127.0.0.1 --port 5173) &
UI_PID=$!
echo "console: http://127.0.0.1:5173/   api: http://127.0.0.1:8000/docs${KNOWLEDGE_URL:+   knowledge: $KNOWLEDGE_URL}${ANALYTICS_URL:+   analytics: $ANALYTICS_URL}   (Ctrl-C stops all)"
wait $UI_PID

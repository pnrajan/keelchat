#!/usr/bin/env bash
# Run the Knowledge Service locally (the same code the deployed service runs).
#
#   scripts/knowledge.sh                       # serve on :8001, stores/embedder per .env (fakes when unset)
#   scripts/knowledge.sh --index examples/knowledge   # also index every .md/.txt in that dir once the service is up
#   scripts/knowledge.sh --port 9001 --fake-embedder   # hashed fake embedder + in-memory index (the model stays real)
#   scripts/knowledge.sh --index DIR --url https://knowledge.example.com   # index into a deployed service, no local server
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=8001 INDEX= URL=
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT=${2:?--port needs a value}; shift 2 ;;
    --index) INDEX=${2:?--index needs a value}; shift 2 ;;
    --url) URL=${2:?--url needs a value}; shift 2 ;;
    --fake-embedder) export SILICONFLOW_API_KEY= KNOWLEDGE_S3_BUCKET= KNOWLEDGE_INDEX_URI=; shift ;;
    -h|--help) sed -n 2,8p "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

index() {  # $1 = base url
  local files=()
  while IFS= read -r -d '' f; do files+=("$f"); done < <(find "$INDEX" -type f \( -name '*.md' -o -name '*.txt' \) -print0 | sort -z)
  [ ${#files[@]} -gt 0 ] || { echo "no .md/.txt files under $INDEX" >&2; return 1; }
  uv run yachat knowledge add "${files[@]}" --url "$1"
}

if [ -n "$URL" ]; then          # remote service only
  [ -n "$INDEX" ] || { echo "--url needs --index DIR" >&2; exit 2; }
  index "$URL"; exit 0
fi

[ -n "${YACHAT_SYNCED:-}" ] || uv sync -q
uv run yachat knowledge serve --host 127.0.0.1 --port "$PORT" &
PID=$!
trap 'kill $PID 2>/dev/null || true' EXIT INT TERM
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 0.5; done
curl -sf "http://127.0.0.1:$PORT/health" >/dev/null || { echo "knowledge service did not start" >&2; exit 1; }
echo "knowledge: http://127.0.0.1:$PORT/   (docs: /docs, health: /health)"
[ -z "$INDEX" ] || index "http://127.0.0.1:$PORT"
wait $PID

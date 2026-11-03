#!/usr/bin/env bash
# Run the Analytics Service locally (conversational analytics over structured datasets).
#
#   scripts/analytics.sh                 # serve on :8002; sources may live under examples/data; SQL answers only
#   scripts/analytics.sh --sandbox       # also run statistics code in Docker (builds deploy/sandbox once)
#   scripts/analytics.sh --port 9002 --data-root /path/to/data
set -euo pipefail
cd "$(dirname "$0")/.."

PORT=8002 DATA_ROOT="$PWD/examples/data" SANDBOX=
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT=${2:?--port needs a value}; shift 2 ;;
    --data-root) DATA_ROOT=$(cd "${2:?--data-root needs a value}" && pwd); shift 2 ;;
    --sandbox) SANDBOX=docker; shift ;;
    -h|--help) sed -n 2,6p "$0"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

export YACHAT_DATA_ROOT="${YACHAT_DATA_ROOT:-$DATA_ROOT}"
if [ -n "$SANDBOX" ]; then
  command -v docker >/dev/null || { echo "--sandbox needs docker" >&2; exit 1; }
  docker image inspect yachat-sandbox:latest >/dev/null 2>&1 || docker build -q -t yachat-sandbox:latest deploy/sandbox
  export YACHAT_SANDBOX=local YACHAT_SANDBOX_BACKEND=docker
fi
[ -n "${YACHAT_SYNCED:-}" ] || uv sync -q
exec uv run yachat analytics serve --host 127.0.0.1 --port "$PORT"

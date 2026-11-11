#!/usr/bin/env bash
# Run the whole stack in Docker on this machine: the API with the console, Redis and Postgres (docker-compose.yml,
# `app` profile). Needs Docker and a .env with a model key (./setup.sh writes one).
#
#   scripts/local.sh up       # build the image, start everything, wait until healthy, print the console URL
#   scripts/local.sh logs     # follow the API's logs
#   scripts/local.sh down     # stop (data kept in volumes)
#   scripts/local.sh reset    # stop and delete the data volumes
#
# KEELCHAT_PORT picks the host port (default 8000, or the next free one; REDIS_PORT and POSTGRES_PORT likewise).
# Tools without a backend URL answer with sample data; set KEELCHAT_TOOLS= (empty) to turn that off.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v docker >/dev/null || { echo "docker is required: run ./setup.sh --docker, or see https://docs.docker.com/get-docker/" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Docker is installed but not running: start Docker Desktop (or the docker service) and try again" >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "docker compose v2 is required: https://docs.docker.com/compose/install/" >&2; exit 1; }
compose() { docker compose --profile app "$@"; }
taken() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
free_from() { local p=$1; while taken "$p"; do p=$((p + 1)); done; echo "$p"; }

case "${1:-up}" in
  up)
    [ -f .env ] || { echo "no .env: run ./setup.sh, or cp .env.example .env and set KEELCHAT_LLM and its key" >&2; exit 1; }
    # host ports: the ones this stack already has, else the next free ones (another Postgres, Redis or server may hold
    # the defaults)
    port_of() { compose port "$1" "$2" 2>/dev/null | sed 's/.*://'; }
    KEELCHAT_PORT=${KEELCHAT_PORT:-$(port_of keelchat 8080)} REDIS_PORT=${REDIS_PORT:-$(port_of redis 6379)} POSTGRES_PORT=${POSTGRES_PORT:-$(port_of postgres 5432)}
    export KEELCHAT_PORT=${KEELCHAT_PORT:-$(free_from 8000)} REDIS_PORT=${REDIS_PORT:-$(free_from 6379)} POSTGRES_PORT=${POSTGRES_PORT:-$(free_from 5432)}
    compose up -d --build --wait
    port=$(compose port keelchat 8080 | sed 's/.*://')
    cat <<MSG

  keelchat is running in Docker: http://127.0.0.1:$port/console/   (API docs: http://127.0.0.1:$port/docs)
  Sessions in Redis, memory and versions in Postgres; data survives restarts.

  scripts/local.sh logs     follow the logs
  scripts/local.sh down     stop

MSG
    ;;
  logs) compose logs -f keelchat ;;
  down) compose down ;;
  reset) compose down -v ;;
  -h|--help) sed -n 2,11p "$0" ;;
  *) echo "unknown command: $1 (up, logs, down, reset)" >&2; exit 2 ;;
esac

#!/usr/bin/env bash
# Local Redis + Postgres in Docker (docker-compose.yml). The schema is created by the app on first use.
#
#   scripts/db.sh up        # start both, wait until healthy, print the .env lines to use
#   scripts/db.sh down      # stop (data kept in volumes)
#   scripts/db.sh reset     # stop and delete the volumes
#   scripts/db.sh psql      # open a psql shell
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo "docker is required: https://docs.docker.com/get-docker/" >&2; exit 1; }
case "${1:-up}" in
  up)
    docker compose up -d --wait
    cat <<'MSG'
databases ready. Add to .env:
  REDIS_URL=redis://127.0.0.1:6379/0
  DATABASE_URL=postgresql://keelchat:keelchat@127.0.0.1:5432/keelchat
MSG
    ;;
  down) docker compose down ;;
  reset) docker compose down -v ;;
  psql) docker compose exec postgres psql -U keelchat keelchat ;;
  *) sed -n 2,7p "$0"; exit 2 ;;
esac

#!/bin/sh
# Take ownership of mounted volumes (container hosts mount them root-owned), then run as the unprivileged `app` user.
set -e
if [ "$(id -u)" = "0" ]; then
  for d in /data; do [ -d "$d" ] && chown -R app "$d" 2>/dev/null || true; done
  exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"

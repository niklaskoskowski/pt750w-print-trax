#!/bin/sh
# Starts as root only to hand /data to PUID:PGID — whoever created ./data on
# the host (docker, sudo, another user) — then drops to that user for good.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
DATA="${PTB_DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
  mkdir -p "$DATA"
  chown -R "$PUID:$PGID" "$DATA" || echo "warning: cannot chown $DATA to $PUID:$PGID" >&2
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

exec "$@"

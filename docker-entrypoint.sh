#!/bin/sh
# Runs as root just long enough to fix /data ownership, then drops to PUID:PGID
# (Unraid convention: 99:100 = nobody:users).
set -e
mkdir -p "$KDS_DATA_DIR"
if [ "$(id -u)" = "0" ]; then
    if [ "$(stat -c %u:%g "$KDS_DATA_DIR")" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" "$KDS_DATA_DIR"
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"

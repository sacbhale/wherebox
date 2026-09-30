#!/bin/sh
# Starts uvicorn as PUID:PGID. Trusted proxies come from uvicorn's FORWARDED_ALLOW_IPS env var.
set -eu

DATA_DIR="${DATA_DIR:-/data}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
export DATA_DIR

mkdir -p "$DATA_DIR/photos" "$DATA_DIR/backups"

set -- uvicorn app.main:create_app --factory \
    --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers "$@"

if [ "$(id -u)" = 0 ]; then
    # Fix only mis-owned files (never a blanket chown -R); -h never follows symlinks.
    if ! find "$DATA_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) \
        -exec chown -h "$PUID:$PGID" {} +; then
        echo "entrypoint: WARNING: could not chown $DATA_DIR to $PUID:$PGID" \
            "(NFS/CIFS root_squash?); continuing. Consider compose 'user:'." >&2
    fi
    echo "entrypoint: dropping privileges to $PUID:$PGID"
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

echo "entrypoint: running as $(id -u):$(id -g); skipping chown and privilege drop"
exec "$@"

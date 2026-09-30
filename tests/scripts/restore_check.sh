#!/usr/bin/env bash
# AC-33 restore check: a copy of the data directory restores to an identical export, and
# backups/latest.db is written by the backup tick.
#
# Run from the repo root:  bash tests/scripts/restore_check.sh
# Needs docker (with compose), curl and python3 on the host. Uses its own compose project
# (inventory-restore-check) on 127.0.0.1:18000, so it does not touch the main stack.
set -euo pipefail

cd "$(dirname "$0")/../.."

PROJECT=inventory-restore-check
BASE=http://127.0.0.1:18000
ORIGIN=$BASE
PASSWORD=restore-test

WORK=$(mktemp -d)
export RESTORE_UID RESTORE_GID RESTORE_DATA_DIR
RESTORE_UID=$(id -u)
RESTORE_GID=$(id -g)
DATA1=$WORK/data1
DATA2=$WORK/data2
JAR=$WORK/cookies.txt
mkdir -p "$DATA1"

compose() {
    docker compose -p "$PROJECT" -f docker-compose.yml -f tests/scripts/compose.restore.yml "$@"
}

cleanup() {
    status=$?
    if [ "$status" -ne 0 ] && [ -n "${RESTORE_DATA_DIR:-}" ]; then
        echo "--- container logs ---" >&2
        compose logs --no-color --tail 80 >&2 || true
    fi
    RESTORE_DATA_DIR=${RESTORE_DATA_DIR:-$DATA1} compose down --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK"
    exit "$status"
}
trap cleanup EXIT

step() { echo "==> $*"; }
fail() {
    echo "FAIL: $*" >&2
    exit 1
}

wait_healthy() {
    for _ in $(seq 1 60); do
        if curl -fsS "$BASE/healthz" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    fail "the app did not answer /healthz within 60s"
}

login() {
    rm -f "$JAR"
    code=$(curl -sS -o /dev/null -w '%{http_code}' -c "$JAR" -H "Origin: $ORIGIN" \
        --data-urlencode "password=$PASSWORD" "$BASE/login")
    [ "$code" = 303 ] || fail "login returned HTTP $code (expected 303)"
    grep -q inv_session "$JAR" || fail "login did not set the inv_session cookie"
}

# Form POST with the session cookie and a matching Origin; prints the HTTP status.
post() {
    local path=$1
    shift
    curl -sS -o "$WORK/last_response" -w '%{http_code}' -b "$JAR" -H "Origin: $ORIGIN" "$@" \
        "$BASE$path"
}

export_json() {
    curl -fsS -b "$JAR" "$BASE/api/export.json" -o "$1"
}

# Prints the id of the live location named $2 in export file $1.
location_id() {
    python3 -c '
import json, sys
doc = json.load(open(sys.argv[1]))
print(next(l["id"] for l in doc["locations"] if l["name"] == sys.argv[2] and not l["deleted"]))
' "$1" "$2"
}

start() {
    RESTORE_DATA_DIR=$1
    compose up -d --build
    wait_healthy
}

step "starting the first container on $DATA1"
start "$DATA1"
login

step "waiting for the startup backup tick to write backups/latest.db"
for _ in $(seq 1 30); do
    [ -f "$DATA1/backups/latest.db" ] && break
    sleep 1
done
[ -f "$DATA1/backups/latest.db" ] || fail "backups/latest.db missing after the startup tick"

step "seeding 2 locations, 5 items and 1 photo capture"
code=$(post /locations --data-urlencode "name=Garage")
[ "$code" = 303 ] || fail "creating Garage returned HTTP $code"
export_json "$WORK/seed.json"
GARAGE=$(location_id "$WORK/seed.json" Garage)
code=$(post /locations --data-urlencode "name=Shelf 2" --data-urlencode "parent_id=$GARAGE")
[ "$code" = 303 ] || fail "creating Shelf 2 returned HTTP $code"
export_json "$WORK/seed.json"
SHELF=$(location_id "$WORK/seed.json" "Shelf 2")

add_item() {
    code=$(post /items/new --data-urlencode "name=$1" --data-urlencode "quantity=$2" \
        --data-urlencode "kind=$3" --data-urlencode "location_id=$4" \
        --data-urlencode "category=$5" --data-urlencode "notes=$6" \
        --data-urlencode "price=$7" --data-urlencode "expiry_date=$8")
    [ "$code" = 303 ] || fail "adding item '$1' returned HTTP $code: $(head -c 500 "$WORK/last_response")"
}
add_item "Cordless drill" 1 belonging "$GARAGE" Tools "18V, blue case" "89.99" ""
add_item "Wood screws" 200 consumable "$SHELF" Hardware "" "" ""
add_item "AA batteries" 12 consumable "$SHELF" Electronics "" "7.50" "2031-01-31"
add_item "Garden hose" 1 belonging "$GARAGE" Garden "Ünïcode note ›" "" ""
add_item "Paint (white)" 2 consumable "$SHELF" "" "half full" "" "2027-06-30"

code=$(post /captures/photo -H "HX-Request: true" -F "location_id=$SHELF" \
    -F "photo=@tests/fixtures/shelf.jpg;type=image/jpeg")
[ "$code" = 200 ] || fail "photo capture returned HTTP $code: $(head -c 500 "$WORK/last_response")"
CAPTURE=$(grep -o 'id="capture-[0-9]*"' "$WORK/last_response" | head -n1 | tr -dc 0-9)
[ -n "$CAPTURE" ] || fail "no capture id in the capture response"

step "waiting for capture $CAPTURE to finish (fake provider)"
final=""
for _ in $(seq 1 60); do
    code=$(curl -sS -o "$WORK/status" -w '%{http_code}' -b "$JAR" -H "HX-Request: true" \
        "$BASE/captures/$CAPTURE/status")
    if [ "$code" = 286 ]; then
        final=yes
        break
    fi
    [ "$code" = 200 ] || fail "capture status returned HTTP $code"
    sleep 1
done
[ -n "$final" ] || fail "capture $CAPTURE did not finish within 60s"

export_json "$WORK/export1.json"
python3 - "$WORK/export1.json" <<'PY' || fail "seeded export has unexpected contents"
import json, sys
doc = json.load(open(sys.argv[1]))
names = sorted(l["name"] for l in doc["locations"] if not l["deleted"])
assert names == ["Garage", "Inbox", "Shelf 2"], names
items = [i for i in doc["items"] if not i["deleted"]]
manual = [i for i in items if i["capture_id"] is None]
captured = [i for i in items if i["capture_id"] is not None]
assert len(manual) == 5, len(manual)
assert len(captured) == 12, len(captured)
assert len(doc["photos"]) == 1 and len(doc["photos"][0]["sha256"]) == 64, doc["photos"]
assert {i["photo_id"] for i in captured} == {doc["photos"][0]["id"]}
print(f"    export: {len(doc['locations'])} locations, {len(items)} items, "
      f"{len(doc['photos'])} photo")
PY

step "on-demand backup (cli backup) and integrity check (cli check)"
compose exec -T --user "$RESTORE_UID:$RESTORE_GID" app python -m app.cli backup
compose exec -T --user "$RESTORE_UID:$RESTORE_GID" app python -m app.cli check
compose exec -T --user "$RESTORE_UID:$RESTORE_GID" app python -m app.cli export >"$WORK/cli1.json"

step "stopping the first container and copying the data directory"
compose down --remove-orphans >/dev/null 2>&1
cp -a "$DATA1" "$DATA2"

step "starting a second container on the copy ($DATA2)"
start "$DATA2"
login
export_json "$WORK/export2.json"

step "diffing the exports"
if ! diff -u "$WORK/export1.json" "$WORK/export2.json"; then
    fail "the export from the copied data differs from the original"
fi
echo "    API exports are identical ($(wc -c <"$WORK/export1.json") bytes)"

step "checking the restored copy: integrity, photo files and backups/latest.db contents"
compose exec -T --user "$RESTORE_UID:$RESTORE_GID" app python -m app.cli check
compose exec -T --user "$RESTORE_UID:$RESTORE_GID" app python -m app.cli export >"$WORK/cli2.json"
diff -u "$WORK/cli1.json" "$WORK/cli2.json" >/dev/null || fail "CLI exports differ"
compose exec -T --user "$RESTORE_UID:$RESTORE_GID" app sh -c \
    'rm -rf /tmp/fromlatest && mkdir /tmp/fromlatest &&
     cp /data/backups/latest.db /tmp/fromlatest/inventory.db &&
     DATA_DIR=/tmp/fromlatest python -m app.cli check >&2 &&
     DATA_DIR=/tmp/fromlatest python -m app.cli export' >"$WORK/cli_latest.json"
diff -u "$WORK/cli1.json" "$WORK/cli_latest.json" >/dev/null \
    || fail "backups/latest.db does not contain the seeded data"
echo "    backups/latest.db exports the same document"
python3 - "$WORK/export2.json" "$DATA2" <<'PY' || fail "photo files do not match the export"
import hashlib, json, pathlib, sys
doc = json.load(open(sys.argv[1]))
root = pathlib.Path(sys.argv[2])
for photo in doc["photos"]:
    digest = hashlib.sha256((root / photo["rel_path"]).read_bytes()).hexdigest()
    assert digest == photo["sha256"], photo
print(f"    {len(doc['photos'])} photo file(s) match their sha256")
PY

echo "PASS: restore check"

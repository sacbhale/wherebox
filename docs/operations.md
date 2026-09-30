# Operations

How to install, expose, secure, back up and restore the household inventory. Everything the
app stores lives in one directory, `/data` in the container (`./data` next to
`docker-compose.yml` by default):

| Path | Contents |
|---|---|
| `inventory.db` (+ `-wal`, `-shm`) | SQLite database in WAL mode |
| `photos/YYYY/MM/` | Original uploads, plus `.display.jpg` derivatives for HEIC |
| `backups/latest.db` | The latest consistent snapshot of the database |

## Install

```sh
cp .env.example .env        # then edit .env, at least PUBLIC_BASE_URL (see below)
docker compose up -d --build
docker compose logs -f app  # shows the one-time setup token when no password is set
curl -fsS http://127.0.0.1:8000/healthz
```

The shipped `.env.example` works as-is for a first try on the same machine
(`PUBLIC_BASE_URL=http://localhost:8000`, published on `127.0.0.1:8000`). For real use,
put the app behind an HTTPS reverse proxy (below) and set `PUBLIC_BASE_URL` to the URL the
household will use.

`uvicorn` runs with exactly one worker process, and one background thread handles AI
captures and backups. Do not scale the service to more replicas or workers: the
single-worker rule is what keeps AI spending within one call of the budget.

### File ownership: PUID/PGID or compose `user:`

- **Default.** The container starts as root. The entrypoint changes the owner of any file
  in `/data` that is not owned by `PUID:PGID` (default `1000:1000`), then drops to that
  user with `setpriv`. A root-owned `./data`, as created by `docker compose up` itself,
  works. Set `PUID`/`PGID` in `.env` to the host user that should own the files.
- **Compose `user:`.** Uncomment `user: "1000:1000"` in `docker-compose.yml` (use your
  uid:gid) to start unprivileged. The entrypoint then skips the ownership fix and the
  privilege drop, so `./data` must already be writable by that user
  (`mkdir -p data && sudo chown 1000:1000 data`). Use this with rootless Docker, Podman,
  and hardened hosts.
- **NFS/CIFS with root_squash.** Root in the container cannot change owners on such mounts.
  The entrypoint prints a warning and continues. Set compose `user:` to the uid:gid that
  owns the share instead.

## Reverse proxy and network exposure

The app speaks plain HTTP on port 8000 and expects a reverse proxy to terminate HTTPS.

### Publish on loopback, or `expose` on a shared network

- **Proxy on the host** (Caddy or nginx installed on the machine, Tailscale serve): keep
  the shipped `ports: ["127.0.0.1:8000:8000"]`. Never publish on `0.0.0.0` (a bare
  `"8000:8000"`): that bypasses the proxy and, with Docker's default firewall rules, is
  reachable from the LAN even when a host firewall seems to block it.
- **Proxy in Docker** (Caddy or Traefik containers): remove `ports:` and use
  `expose: ["8000"]` on a network shared with the proxy. Then only containers on that
  network can reach the app.

### Trusted proxies: `FORWARDED_ALLOW_IPS`

uvicorn runs with `--proxy-headers` and honours `X-Forwarded-For` and `X-Forwarded-Proto`
only from addresses in `FORWARDED_ALLOW_IPS`. The client IP drives the login throttle, and
the scheme drives the Secure cookie flag and the CSRF origin check. The default is
`127.0.0.1,172.16.0.0/12`: loopback plus the Docker bridge range, where a proxy container
or the Docker gateway normally appears.

> **Warning: trusting `172.16.0.0/12` is unsafe with Docker's userland-proxy, hairpin NAT,
> rootless Docker (slirp4netns/pasta), or Podman.** In those setups every client,
> including LAN or internet clients, can reach the app from a bridge or gateway address.
> Any client could then send its own `X-Forwarded-For`, pick an IP to evade the per-IP
> login throttle, and claim `https`. Set `FORWARDED_ALLOW_IPS` to the proxy's exact
> address instead, for example `FORWARDED_ALLOW_IPS=172.30.0.2`. Find it with
> `docker network inspect <network>` (proxy container) or `docker network inspect
> <project>_default --format '{{(index .IPAM.Config 0).Gateway}}'` (proxy on the host,
> seen through the gateway). Never use `*`.

A request from an untrusted address is still served. Its forwarded headers are just
ignored, so it counts as the direct peer IP over plain HTTP.

### Caddy

On the host, with the app published on `127.0.0.1:8000`:

```caddyfile
inventory.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

As a container on a network shared with the app (`expose: ["8000"]` on the app):

```caddyfile
inventory.example.com {
    reverse_proxy app:8000
}
```

Caddy sets `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host` by default.
Set `PUBLIC_BASE_URL=https://inventory.example.com`.

### Traefik

The app joins Traefik's network, and Traefik discovers it by labels. Give that network a
fixed subnet, so `FORWARDED_ALLOW_IPS` can name Traefik's exact address:

```yaml
services:
  app:
    # ...as in docker-compose.yml, but with `expose` instead of `ports`:
    expose: ["8000"]
    environment:
      FORWARDED_ALLOW_IPS: 172.30.0.2 # Traefik's address on the proxy network
    networks: [proxy]
    labels:
      traefik.enable: "true"
      traefik.http.routers.inventory.rule: Host(`inventory.example.com`)
      traefik.http.routers.inventory.entrypoints: websecure
      traefik.http.routers.inventory.tls.certresolver: letsencrypt
      traefik.http.services.inventory.loadbalancer.server.port: "8000"
  traefik:
    # ...your Traefik service...
    networks:
      proxy:
        ipv4_address: 172.30.0.2
networks:
  proxy:
    ipam:
      config:
        - subnet: 172.30.0.0/24
```

Traefik sets the `X-Forwarded-*` headers by default.

### Tailscale serve

On the Docker host, with the app published on `127.0.0.1:8000`:

```sh
tailscale serve --bg --https=443 http://127.0.0.1:8000
```

Set `PUBLIC_BASE_URL=https://<machine>.<tailnet>.ts.net`. The connection reaches the
container through the Docker gateway, which the default `FORWARDED_ALLOW_IPS` trusts. Only
local processes can reach a `127.0.0.1` publish, so this is safe unless the host runs
untrusted software. To be exact, set `FORWARDED_ALLOW_IPS` to the gateway address (see the
warning above). If the phone gets no session cookie behind Tailscale, set
`COOKIE_SECURE=true`, because every Tailscale serve URL is HTTPS.

### `COOKIE_SECURE`

- `auto` (default): the session cookie gets the Secure flag when the effective scheme,
  after trusted-proxy handling, is https. Plain-HTTP LAN access such as
  `http://nas.local:8000` still gets a working cookie.
- `true`: always Secure. Use it when the app is only ever reached over HTTPS.
- `false`: never Secure. Only for plain-HTTP setups whose proxy mis-reports the scheme.

### `PUBLIC_BASE_URL`: set it before printing tags

Every location's QR code and NFC tag holds `PUBLIC_BASE_URL/l/<code>`. Choose the final
URL (`scheme://host[:port]`, no path) **before printing any tags**. Changing it later
orphans every printed QR/NFC tag, because the old URL no longer reaches the app. Settings
repeats this warning. The browser's `Origin` must match the `PUBLIC_BASE_URL` host, the
request's Host, or one of the comma-separated names in `ALLOWED_HOSTS`. Otherwise forms
return 403.

## Passwords and login

- **First start without a password.** Every page redirects to `/setup`, which asks for a
  one-time **setup token** printed in the container logs (`docker compose logs app`, look
  for `SETUP TOKEN`). After setup the token is discarded and `/setup` returns 404.
- **`HOUSEHOLD_PASSWORD` in `.env`** is hashed at startup and **replaces the stored
  password on every restart**. It overrides anything set with `set-password`, so remove
  it from `.env` if you manage the password with the CLI.
- **Change the password with the CLI:**

  ```sh
  docker compose exec app python -m app.cli set-password
  ```

  Every password change (setup, env override or CLI) signs out all sessions.
- **Sessions** last `SESSION_DAYS` (30) and are extended while in use.

### Login throttle and its trade-off

- One client IP gets 5 failed logins per 15 minutes, then HTTP 429 with `Retry-After`.
  Other IPs are unaffected.
- Once more than 50 logins fail across all IPs in 15 minutes, every attempt (right or
  wrong, from any IP) waits `min(2s × (failures − 50), 30s)` before the password is
  checked. It **never locks the household out**. A correct password still works, just
  slowly, during an attack.
- The trade-off: a hard lock would let an attacker lock the household out. The progressive
  delay still lets a distributed attacker make about one guess every 30 seconds at the cap.
  That is negligible against an argon2id-hashed **long passphrase**, so use one (four or
  more random words).
- The counters live in memory and **reset on restart** (accepted: restarts are rare, and
  argon2 slows guessing anyway).
- The throttle is only as good as the client IP. See the `FORWARDED_ALLOW_IPS` warning.

## AI capture: provider, cost and budget

- `AI_PROVIDER=anthropic` with `AI_API_KEY` and `AI_MODEL` (a vision-capable Claude
  model). Without a key, AI capture is disabled with a message, and manual entry still
  works.
- **Prices and budget are in USD.** `AI_INPUT_PRICE_PER_MTOK` and
  `AI_OUTPUT_PRICE_PER_MTOK` are USD per million tokens (copy them from the provider's
  price list for your model). `MONTHLY_AI_BUDGET` is USD per calendar month in `TZ` and
  requires both prices. Once the month's total reaches it, capture buttons are disabled
  and the capture endpoints return 409 until next month. Manual entry keeps working.
  Overshoot is at most the one call already in flight.
- **Timeouts are not counted.** A call that hits `AI_TIMEOUT_SECONDS` (60) reports no
  usage, so it is recorded with 0 tokens, although the provider may still bill for it.
  Reported usage and the budget check can therefore undercount. Settings says so as well.
- **Stopping during a call.** `stop_grace_period: 15s` lets uvicorn finish HTTP requests,
  but it is shorter than the 60s provider timeout. `docker compose stop` can therefore
  interrupt an in-flight AI call. The capture stays in the queue, is retried on the next
  start (its upload is on disk), and at most that one call's usage goes unrecorded.
- Failed captures keep their input. **Retry** on the capture resets it. A capture that
  fails 3 attempts in a row stops.

## Backups and restore

### What is backed up automatically

Every `BACKUP_INTERVAL_HOURS` (24), and shortly after startup when no recent backup
exists, the background worker writes `backups/latest.db`:

1. It deletes a stale `backups/latest.db.tmp` left by an interrupted run.
2. It runs `VACUUM INTO 'backups/latest.db.tmp'`. This is a read transaction, so web
   requests and captures continue meanwhile.
3. It fsyncs the file and atomically renames it over `backups/latest.db`.
4. It records the time as `last_backup_at`, which Settings shows. Settings warns when the
   last backup is older than twice the interval.

`latest.db` is therefore always a complete, consistent snapshot, even when `/data` is
copied while the app is writing. To write one now:

```sh
docker compose exec app python -m app.cli backup
```

### Back up

Copy the **whole data directory**, including `photos/` and `backups/`:

```sh
docker compose stop            # best: a quiet copy
cp -a data /path/to/backup/inventory-data-$(date +%F)
docker compose start
```

Copying while the app runs also works. The live `inventory.db` might then be caught
mid-write, but `backups/latest.db` in the copy is always consistent (restore step 3).
`python -m app.cli export` (or `GET /api/export.json` while logged in) prints a JSON
document of all locations (with codes), items and photo paths with sha256. It is useful
as a readable extra copy and for comparing two installs, but restore uses the data
directory.

### Restore

1. Stop the app and put the copied directory in place as `./data` (or wherever the
   compose volume points).
2. Check the database:

   ```sh
   docker compose run --rm --no-deps --entrypoint python app -m app.cli check
   ```

   This runs `PRAGMA integrity_check` on `inventory.db`, prints `ok: ...` and exits 0 when
   the database is healthy. Then start the app with `docker compose up -d`.
3. If the check fails, it exits non-zero, and it names a usable `backups/latest.db` if
   there is one. With the app still stopped, repair:

   ```sh
   docker compose run --rm --no-deps --entrypoint python app -m app.cli check --repair
   ```

   `--repair` only acts when the check fails and `backups/latest.db` itself passes the
   check. It moves the damaged `inventory.db` (and its `-wal`) aside as
   `inventory.db.corrupt-<timestamp>`, deletes `-shm`, and copies `latest.db` into place.
   **Changes made after that backup are lost.** Then start the app. Without `--repair`,
   `check` never modifies anything. The manual equivalent: delete `inventory.db-wal` and
   `inventory.db-shm` first, then copy `backups/latest.db` over `inventory.db`.

   `docker compose run --entrypoint python` runs as root, so the restored file is
   root-owned until the next normal start fixes ownership. With compose `user:`, add
   `--user <uid>:<gid>`.

`bash tests/scripts/restore_check.sh` tests this path end to end. It seeds a container,
copies its data directory, starts a second container on the copy, and checks that the
exports are identical and that `backups/latest.db` holds the same data.

## Scan-to-move in a different browser

Scan-to-move ("Scan destination tag" in an item's `⋯` sheet) stores the pending move in the
**server session of the browser you started it in**. It expires after 10 minutes. The move
applies when that same browser opens the destination's `/l/<code>` URL.

Some phone QR scanners and NFC handlers open URLs in a **different browser** from the one
the household uses, or outside an **installed home-screen web app**, which has its own
cookie jar. That browser has no session cookie. It shows the login page, and after login
it redirects back to the location. The pending move is **not** visible there, because it
belongs to the other browser's session. What to do:

- Open the scanned URL in the browser where the move was started, or scan from inside that
  browser. The move then applies, with undo. If it expired, start it again.
- Or set the phone's default browser to the one the household uses for the inventory.
- Use a normal browser tab rather than an installed home-screen app when you rely on
  scan-to-move.

## Manual verification record

### AC-15: real-key capture timing

AC-15 requires a p50 of 20 seconds or less over 5 real-key shelf-photo captures of about 12
items each, with an empty queue. The automated part (pipeline overhead under 2s with the
fake provider) is covered by `tests/integration/test_capture_photo.py`. This real-key run
is a manual verification item: it needs a real API key and real shelves, so it cannot run
in CI.

**Status: manual check — not yet recorded.**

Procedure:

1. Configure `AI_PROVIDER=anthropic`, `AI_API_KEY`, `AI_MODEL` and the prices, then
   restart. Confirm that no capture is queued or processing (the `/captures` page).
2. On a phone, over the real proxy URL, open a location page and photograph a shelf with
   about 12 distinct items. Start a stopwatch when you tap upload, and stop it when the
   status line shows the result.
3. Wait for each capture to finish before the next one. Repeat for 5 different shelves.
4. Read the server-side timings. `duration_ms` is the provider call alone;
   `total_s` runs from upload to done:

   ```sh
   docker compose exec app python -c "import sqlite3; c = sqlite3.connect('/data/inventory.db'); [print(r) for r in c.execute(\"SELECT id, duration_ms, round((julianday(finished_at) - julianday(created_at)) * 86400, 1) AS total_s, (SELECT count(*) FROM items WHERE capture_id = captures.id) AS items FROM captures WHERE type = 'photo' ORDER BY id DESC LIMIT 5\")]"
   ```

5. Fill in the table and the p50 (the 3rd value when sorted). Replace the status line
   above with the date and a pass or fail result.

| Run | Date | Model | Items found | `duration_ms` | `total_s` | Stopwatch (s) |
|---|---|---|---|---|---|---|
| 1 | not yet recorded | | | | | |
| 2 | not yet recorded | | | | | |
| 3 | not yet recorded | | | | | |
| 4 | not yet recorded | | | | | |
| 5 | not yet recorded | | | | | |
| **p50** | | | | | | |

The other manual checks from the plan are a real iPhone opening the rear camera over HTTPS
through the proxy (AC-32) and a printed QR scanned by a phone (AC-11). Record them the same
way when done.

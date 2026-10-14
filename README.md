# Household inventory

A self-hosted inventory for one household. Items live in a nested tree of locations, and
every location has a permanent short URL (`/l/<code>`) for printed QR codes and NFC tags.
Items can be added by photographing a shelf, a receipt, or by typing or dictating a
sentence: an AI model extracts the items. Manual entry always works too. It runs as one
Docker container with SQLite and a photos directory on one volume, behind your HTTPS
reverse proxy.

- Phone-first pages: search, `+1`/`−1`, "Used up" and move in one or two taps, plus
  scan-to-move (start a move, then scan the destination's tag).
- AI capture runs in a background queue with retry. A review queue collects low-confidence
  items and proposals from reconciling a shelf. Monthly budget in USD.
- One shared household login (login name + argon2id password), 30-day sessions and a login
  throttle.
- Automatic `backups/latest.db` snapshots, a JSON export, and a copy-the-directory
  restore.

## Quick start

```sh
cp .env.example .env     # set PUBLIC_BASE_URL before printing any tags
docker compose up -d --build
docker compose logs app  # the one-time setup token, unless HOUSEHOLD_PASSWORD is set
```

Open http://localhost:8000. For real use behind Caddy, Traefik or Tailscale serve, and
for passwords, AI keys and budget, backups and restore, see
[docs/operations.md](docs/operations.md).

Operator commands run inside the container:

```sh
docker compose exec app python -m app.cli set-password
docker compose exec app python -m app.cli set-username   # the login name (or --name NAME)
docker compose exec app python -m app.cli backup
docker compose exec app python -m app.cli check        # PRAGMA integrity_check
docker compose exec app python -m app.cli export > inventory-export.json
```

## Development

Requires [uv](https://docs.astral.sh/uv/). It installs Python 3.12 and the locked
dependencies.

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/unit tests/integration -q
uv run playwright install --with-deps chromium webkit && uv run pytest tests/e2e -q
bash tests/scripts/restore_check.sh   # Docker: restore from a copied data dir (AC-33)
```

Run locally without Docker (the fake AI provider replays `tests/fixtures/fake_script.json`):

```sh
PUBLIC_BASE_URL=http://127.0.0.1:8000 DATA_DIR=./data \
HOUSEHOLD_USERNAME=dev HOUSEHOLD_PASSWORD=dev \
AI_PROVIDER=fake FAKE_AI_SCRIPT=tests/fixtures/fake_script.json \
uv run uvicorn app.main:create_app --factory --port 8000
```

## Docs

- [docs/operations.md](docs/operations.md): install, file ownership, reverse proxies and
  `FORWARDED_ALLOW_IPS`, cookies, the login name, passwords and the throttle, AI cost and
  budget, backups and restore, the scan-to-move caveat, and the manual verification record.
- [docs/ai-provider-contract.md](docs/ai-provider-contract.md): the AI provider
  interface, error classes and the fake provider's script format.

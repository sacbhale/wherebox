# Plan: Household Inventory with AI Capture (v1)

Status: CONSENSUS APPROVED (iteration 2 + improvements) — APPROVED FOR EXECUTION by user 2026-09-29 (via ralph)
Mode: RALPLAN-DR SHORT, --direct (no interview; requirements from spec)
Source spec: `.omc/specs/deep-interview-household-inventory-ai.md` (AC-1..AC-33)
Date: 2026-09-29. Repo: `/var/ai/git-repos/inventory-app` (greenfield, not yet a git repo)

---

## 1. Requirements Summary

This is a self-hosted inventory for one household.
- Items live in a nested location tree.
- Items are added by AI from photos, receipts and text or voice, or entered manually.
- Every location has a permanent short-code URL for QR and NFC tags.
- It runs as one Docker container with SQLite and a photos directory on one volume, behind the user's HTTPS reverse proxy.

- Inventory core: AC-1..AC-12 (item model, location tree, search under 1s, filters, browse, 2-tap quick actions, review queue, manual form, permanent codes, login redirect, QR and URL, removed-location page).
- Item capture: AC-13..AC-26 (location-scoped photo capture, confidence and needs_review, about 20s for 12 items, local photo storage, receipts to Inbox, chat text, browser voice, reconcile with confirmed removals, provider interface, 1500px downscale, usage tracking, monthly budget stop, pending and retry, a documented contract with a test double).
- Household and accounts: AC-27..AC-33 (auth on every route, hashed shared password, 30-day sessions, login throttling, correct proxy handling, one-handed phone use with the rear camera, `docker compose up`, directory-copy backup).
- Out of scope: AI assistant and chat Q&A, notifications, email ingestion, per-user accounts, a local model adapter, NFC writing, barcodes.

### Resolved decisions (from review)
- `quantity` is an INTEGER. `-1` stops at 0 and keeps the item; "Used up" is the only action that removes it.
- Deleting a non-empty location first shows a confirmation with counts. It then re-parents the child locations and direct items to the parent (or to Inbox for items when the deleted location is a root). Child locations keep their short codes.
- `MONTHLY_AI_BUDGET` is in USD and labeled as USD in Settings.

### Interpretations confirmed by user (2026-09-29)
- **AC-6 (Move in 2 taps): accepted, plus scan-to-move.** Recent, parent and sibling locations take 2 taps (tap `⋯`, then tap the destination). Other destinations go through the action-sheet search. **Scan-to-move (user addition):** `⋯` then "Scan destination tag" stores a pending move in the server session (10-minute expiry). The user then scans a container's QR code or taps its NFC tag with the phone, which opens `/l/<code>` in the browser. Arriving there with a pending move applies it immediately and shows an undo toast. That is 2 taps plus the scan, and it works for any destination.
- **AC-15 (about 20s): accepted with one serial worker.** The target applies to a capture that starts right away. A capture queued behind others waits for them, and the status partial shows its queue position.

## 2. RALPLAN-DR Summary

### Principles
1. **Boring and single-process.** One container, one process, one SQLite file, one worker thread. No external queue or cache, and no Node build step.
2. **Never lose input.** Store every photo, receipt and text before any AI call. An AI failure only degrades the capture to "pending with retry".
3. **Identity is permanent.** A location short code is immutable and never reused. The database enforces this, and tests check the enforcement after every migration.
4. **AI behind a narrow contract.** Callers depend on one provider interface. Adapters can be swapped (cloud now, Ollama later), and tests use a deterministic fake.
5. **Phone first, with vetted security patterns.** Server-rendered pages using native browser features. Security mechanisms copy established framework patterns (Django-style Origin, Host and proxy handling) instead of inventing new ones.

### Decision Drivers (top 3)
1. **Operational simplicity** for a self-hoster: small image, one process, trivial backup.
2. **Implementation speed and low maintenance** for a single-household app with about 15 screens.
3. **Strong image, PDF and AI tooling:** downscaling, HEIC, PDF rasterizing, QR, and a provider SDK, with full control over SQLite (triggers, FTS5).

### Viable Options (key choice: stack and app shape)

**Option A: full-stack TypeScript (SvelteKit 2 on Node 22, better-sqlite3, sharp)**
- Pros: one language end to end, rich client interactivity, fast image processing with sharp (prebuilt arm64 binaries exist for sharp and better-sqlite3), a good Anthropic TS SDK.
- Cons: needs a Vite build step and adapter-node in the image and CI. PDF-to-image rasterizing in Node (pdfjs plus node-canvas or an external binary) is the weak link for AC-17. It also carries more client state than this app needs.

**Option B: Python FastAPI JSON API plus a Vite/Preact SPA**
- Pros: strong Python imaging and PDF stack, clean API boundary.
- Cons: two codebases and an SPA build, duplicated validation, and client-side routing and auth redirects for `/l/{code}` deep links. That is about twice the surface area for no v1 benefit.

**Option C: Python FastAPI with server-rendered Jinja2 and htmx (CHOSEN)**
- Pros: one codebase and no JS build (htmx is vendored as a file). Deep links are ordinary server routes. Pillow, pillow-heif, pypdfium2 and segno cover images, HEIC, PDF and QR, with arm64 wheels. Raw SQL gives direct control of triggers and FTS5. An in-process worker thread is enough.
- Cons: CSRF, sessions and proxy handling have to be hand-assembled, which is mitigated by copying Django's patterns (section 3.7). Client interactivity is less rich. About 150 lines of vanilla JS are needed.

**Option D: Django 5 with htmx (steelmanned)**
- Pros: built-in and battle-tested CSRF, sessions, `SECURE_PROXY_SSL_HEADER`, `ALLOWED_HOSTS` and password hashing (argon2). ModelForms cover AC-8 almost for free. Management commands (backup, set-password) and a mature migration framework. The admin is a free fallback UI. It matches the server-rendered shape of C.
- Cons: on SQLite, Django's `AlterField` and similar operations run a table-rebuild migration (create new table, copy, drop, rename). That silently drops custom triggers, so the short-code triggers would vanish on an ordinary model change. This is mitigable with a `post_migrate` handler that runs `CREATE TRIGGER IF NOT EXISTS`, plus a presence test. Django 5.1+ also supports `transaction_mode="IMMEDIATE"`. The ORM has no FTS5 support, so search becomes raw SQL beside the ORM, and FTS sync through signals misses `QuerySet.update()`.

**Invalidation rationale and synthesis:**
- B is rejected because it doubles the codebase and build pipeline (Driver 2).
- A is rejected on Driver 1 (build step) and Driver 3 (PDF rasterizing). Native binaries are not the issue.
- D is the strongest alternative, and C vs D is **close: a judgment call**. D's trigger loss is mitigable (post_migrate re-create plus a presence test), and C needs the same never-rebuild discipline anyway. The deciding factors are direct control of the SQL, transactions and FTS5 for C, and a smaller dependency surface. Against that, D has framework-vetted security. C wins narrowly.
- Synthesis: keep C, but borrow D's vetted patterns: Origin and Referer CSRF checks with trusted hosts, a proxy SSL header trust list, a hashed session store, and management-style CLI commands.

## 3. Proposed Architecture

### 3.1 Stack (pinned majors)
- Python **3.12**.
- FastAPI **0.11x**.
- Uvicorn **>=0.30** with CIDR support in `FORWARDED_ALLOW_IPS`. Step 2 verifies this against the pinned version, and a test covers it.
- python-multipart **0.0.x** for uploads, Jinja2 **3.x**, Pydantic **2.x** and pydantic-settings **2.x**.
- htmx **2.x**, vendored as `app/static/vendor/htmx.min.js`. Plain CSS, vanilla JS.
- SQLite through stdlib `sqlite3` (bookworm libsqlite3 3.40, which has FTS5). No ORM: thin repository modules and raw SQL.
- Pillow **11.x**, pillow-heif **0.2x**, pypdfium2 **4.x**, segno **1.6.x**, argon2-cffi **23.x**.
- `anthropic` Python SDK, used only inside `app/ai/`.
- Dev dependencies: `uv` (with `uv.lock`), ruff, pytest **8.x**, pytest-playwright, **zxing-cpp** (QR decoding in tests).
- Container: `python:3.12-slim-bookworm`, multi-arch (amd64 and arm64).

### 3.2 Repo layout (planned paths)
```
pyproject.toml  uv.lock  Dockerfile  docker-entrypoint.sh  docker-compose.yml  .env.example  .dockerignore  README.md
docs/ai-provider-contract.md  docs/operations.md
app/main.py            # app factory, middleware, routers, lifespan (worker start unless WORKER_AUTOSTART=false)
app/config.py          # Settings, startup validation
app/db/connection.py   # connect(): isolation_level=None + PRAGMAs; write_tx() BEGIN IMMEDIATE context manager
app/db/migrate.py      # ordered *.sql + Python post-migration hooks; schema_migrations
app/db/migrations/0001_init.sql
app/auth/{passwords,sessions,throttle,csrf,middleware,routes}.py
app/inventory/{shortcode,locations,items,categories,search,routes}.py
app/capture/{images,service,worker,apply,reconcile,budget,routes}.py
app/ai/{provider,schemas,prompts,anthropic_adapter,fake,registry}.py
app/api/routes.py      # /api/export.json, /api/items/{id} (JSON)
app/qr.py  app/cli.py  # cli: backup | export | set-password | check
app/settings_info.py   # Settings page data: AI month total, photo-dir size, warnings
app/templates/...  app/templates/partials/...  app/static/{app.css,app.js,voice.js,vendor/}
tests/{conftest.py,unit/,integration/,e2e/,fixtures/fake_script.json}
tests/scripts/{restore_check.sh,compose.restore.yml}
```

### 3.3 Data model (`0001_init.sql`, plus a post-migration hook)

**`locations`**
- Columns: `id INTEGER PK`, `short_code TEXT NOT NULL UNIQUE`, `name TEXT NOT NULL`, `parent_id INTEGER NULL REFERENCES locations(id)`, `is_system INTEGER NOT NULL DEFAULT 0`, `deleted_at TEXT NULL`, `created_at`, `updated_at`.
- **Immutability:**
  - `CREATE TRIGGER locations_code_immutable BEFORE UPDATE OF short_code ON locations WHEN OLD.short_code IS NOT NEW.short_code BEGIN SELECT RAISE(ABORT,'short_code immutable'); END;`
  - `CREATE TRIGGER locations_no_delete BEFORE DELETE ON locations BEGIN SELECT RAISE(ABORT,'locations are soft-deleted'); END;`
  - Every connection sets `PRAGMA recursive_triggers=ON`, so the conflict-resolution delete done by `REPLACE` also fires the delete trigger.
  - A grep test also bans `REPLACE`/`INSERT OR REPLACE` against `locations` anywhere in `app/`.
- **Migration rule (written in `app/db/migrations/README` and enforced by tests):** migrations never rebuild or drop `locations`. Only `ALTER TABLE ... ADD COLUMN` is allowed. A test asserts that both triggers exist in `sqlite_master` after the latest migration and that sampled codes are unchanged.
- The README also notes that `recursive_triggers` is a per-connection PRAGMA. An external `sqlite3` shell does not set it, so `REPLACE` issued there bypasses the delete protection. Manual DB surgery must never use REPLACE on `locations`.
- **Inbox:** migrate.py's Python post-migration hook `ensure_inbox()` inserts the single `is_system=1` "Inbox" row, with a code from `shortcode.generate()`, if none exists. Inbox cannot be renamed, moved or deleted.
- **Tree queries:** subtrees use `parent_id` with recursive CTEs (under 2k locations, under 5ms). The move cycle guard collects the new parent's ancestors and rejects the move (400) if they include the moved id (AC-2).
- **Soft delete:** sets `deleted_at` and re-parents contents, per the resolved decision above.

**Other tables**
- `categories(id PK, name TEXT NOT NULL UNIQUE COLLATE NOCASE)`.
- `items(id PK, name NOT NULL, quantity INTEGER NOT NULL DEFAULT 1 CHECK(quantity>=0), category_id NULL, kind NOT NULL CHECK(kind IN('consumable','belonging')), location_id NOT NULL, expiry_date NULL, purchase_date NULL, price_cents NULL, notes NULL, photo_id NULL, capture_id NULL, confidence REAL NULL, needs_review INTEGER NOT NULL DEFAULT 0, last_seen_at NULL, deleted_at NULL, created_at, updated_at)` (AC-1). "Used up" sets `deleted_at` and shows an undo toast.
- `photos(id PK, capture_id, rel_path, display_rel_path NULL, sha256, width, height, created_at)`. For HEIC, `display_rel_path` points to a JPEG derivative generated in the worker's prepare step; the original is kept.
- `captures(id PK, type CHECK IN('photo','receipt','text','reconcile'), status CHECK IN('queued','processing','done','failed'), location_id, input_text NULL, merchant NULL, error NULL, attempts INTEGER NOT NULL DEFAULT 0, not_before TEXT NOT NULL /*UTC*/, provider, model, duration_ms, created_at, finished_at)`.
- `ai_calls(id PK, capture_id, started_at TEXT /*UTC ISO*/, input_tokens, output_tokens, cost_micros, outcome CHECK IN('ok','invalid_output','transient_error','permanent_error','timeout'))`. This is the authoritative usage ledger (AC-23 and AC-24).
- `review_proposals(id PK, type CHECK IN('missing','quantity'), item_id, capture_id, proposed_quantity INTEGER NULL, status CHECK IN('open','accepted','dismissed'), created_at, resolved_at)`, with `CREATE UNIQUE INDEX one_open_proposal ON review_proposals(item_id) WHERE status='open'`.
- `sessions(token_hash PK, created_at, expires_at, last_seen_at)`.
- `settings(key PK, value)`: password_hash, last_backup_at.

**Search (FTS5)**
- `items_fts` is a **regular** FTS5 table that stores its own content: `CREATE VIRTUAL TABLE items_fts USING fts5(name, category, notes, location_path, tokenize='unicode61 remove_diacritics 2')`, where `rowid = items.id`.
- There are no triggers. `search.reindex_items(conn, ids)` is **set-based** and makes 2 statements total, never one CTE per item:
  1. `DELETE FROM items_fts WHERE rowid IN (ids)`.
  2. One `INSERT INTO items_fts(rowid,name,category,notes,location_path) SELECT ...`, where a single `WITH RECURSIVE paths(id, path)` CTE computes every live location's path once, joined to `items` (live rows in `ids`) and `categories`.
- `ids` is passed through a temp table (`temp.reindex_ids`), not by building a huge `IN` list.
- It is called inside the same write transaction as:
  - item create, edit and move;
  - "Used up" undo, which restores the row;
  - location rename and move (all item ids in the subtree);
  - location delete (re-parented items);
  - category rename or merge (all items in the affected categories).
- Soft-delete of an item removes its FTS row.
- **Query building:** each whitespace token has `"` doubled and is wrapped as a quoted prefix phrase (`"batt"*`), and tokens are joined with spaces (implicit AND). Input such as `"`, `AND` or `*(` therefore can never raise an FTS syntax error. A unit test fuzzes this.

**Connections and PRAGMAs**
- `connect()` uses `isolation_level=None` and sets `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL` and `recursive_triggers=ON`.
- All writes, web and worker, go through `with write_tx(conn):`, which runs `BEGIN IMMEDIATE`, then COMMIT, or ROLLBACK on exception. No writes are made outside this helper, and a grep test flags bare `conn.execute("INSERT|UPDATE|DELETE"...)` outside the repository modules.

### 3.4 Short codes and location URLs
- **Codes:** 6 Crockford base32 characters (alphabet `0-9A-Z` minus `ILOU`) generated with `secrets`, retrying up to 5 times on a UNIQUE conflict.
- **Lookup:** `/l/{code}` normalizes the code (uppercase, `O→0`, `I/L→1`, strip `-`).
  - A live code shows the location page (AC-10).
  - A deleted code returns 410 with `location_removed.html` (AC-12).
  - An unknown code returns 404.
- **Permanent URL:** `PUBLIC_BASE_URL + '/l/' + code`. `PUBLIC_BASE_URL` must be scheme plus host (and optional port) with **no path**; a path is a startup error. Settings shows its value with a warning: "Changing this orphans every printed QR/NFC tag."
- **QR (segno, error level M):**
  - `GET /l/{code}/qr.svg`
  - `GET /l/{code}/qr.png?scale=10` (served as an attachment)
  - `GET /l/{code}/label` (printable, with the URL as text under the QR)
- The location page has a copy button (clipboard API, with a select-text fallback) (AC-11).

### 3.5 AI provider contract (`app/ai/provider.py`, documented in `docs/ai-provider-contract.md`)
```python
class AIProvider(Protocol):
    name: str; model: str
    def extract(self, req: ExtractRequest) -> ExtractResult: ...   # sync; called from worker thread only
ExtractRequest: mode: Literal['shelf','receipt','text','reconcile']; images: list[bytes] (JPEG, long edge <=1500)
                text: str|None; known_locations: list[str]; existing_items: list[ExistingItem(id,name,quantity)]; timeout_s
ExtractResult:  items: list[ExtractedItem(name, quantity>=1, category, kind, confidence 0..1, location_hint|None,
                price_cents|None, purchase_date|None, matched_existing_id|None)]
                receipt: ReceiptMeta(merchant|None, purchase_date|None)|None; usage: Usage(input_tokens, output_tokens)
Errors:         ProviderTransientError(usage|None), ProviderPermanentError(usage|None), ProviderInvalidOutput(usage, raw)
```
- **Usage reporting:** adapters report usage even when their output fails validation (`ProviderInvalidOutput` carries it), so billed-but-failed calls reach the ledger.
- **`anthropic_adapter.py`:** Messages API with image blocks and one forced tool call whose input schema is the items and receipt schema. The adapter maps SDK exceptions and timeouts to the error classes above.
- **`fake.py`:** `FakeProvider` is scripted with a queue of results or exceptions, an optional delay and usage, and records every request. `AI_PROVIDER=fake` loads `FAKE_AI_SCRIPT` (JSON).
- **`registry.py`:** `get_provider(settings)` returns `anthropic` or `fake`. A future `ollama` adapter is one module plus one registry entry (AC-21).
- **Cost:** `cost_micros = input_tokens*AI_INPUT_PRICE_PER_MTOK + output_tokens*AI_OUTPUT_PRICE_PER_MTOK`, both prices taken from env. No price or model table is hardcoded.

### 3.6 Capture pipeline (background, durable, one worker)
**Invariant:** exactly one worker thread makes provider calls (uvicorn `--workers 1`, plus one thread started in the lifespan). This invariant is what bounds budget overshoot to at most one in-flight call. Any future parallelism must add a reservation step first.

1. **Request** (`capture/routes.py`). Requires `HX-Request`, passes CSRF and checks the budget.
   - If the budget is exhausted, it returns 409 with a message partial, and the capture buttons render disabled (AC-24).
   - Otherwise it saves the original upload(s) to `photos/YYYY/MM/<uuid>.<ext>` and inserts `captures(status='queued', not_before=now)` inside `write_tx`. Uploads do no image decoding in the request.
   - It then returns a status partial showing the queue position (the count of queued captures ahead). The partial polls `GET /captures/{id}/status` every 2s, and that endpoint returns **HTTP 286** (htmx stop-polling) with the final partial once the capture is `done` or `failed`.
2. **Claim.** Inside `write_tx`: `UPDATE captures SET status='processing', attempts=attempts+1 WHERE id=(SELECT id FROM captures WHERE status='queued' AND not_before <= :now ORDER BY created_at LIMIT 1)`.
   - If `attempts > 3`, the capture is set to `failed` with "gave up after 3 attempts", which stops a poison input from crash-looping.
   - On startup, `processing` captures are reset to `queued`; their attempts count is kept.
   - **Shutdown:** `docker stop` sends SIGTERM and allows 10s, which is shorter than the 60s provider timeout, so an in-flight call can be killed. That is safe: the capture stays `processing`, is re-queued on the next start, and its input is on disk. The compose file sets `stop_grace_period: 15s` so uvicorn can finish HTTP requests.
3. **Prepare** (`images.py`).
   - `Image.MAX_IMAGE_PIXELS = 50_000_000`, EXIF transpose, HEIC decoded to RGB. The HEIC JPEG display derivative is written here, in the worker, and saved to `photos.display_rel_path`.
   - PDF: at most 3 pages rasterized via pypdfium2 at 150dpi. A PDF over 20 pages or 20MB is rejected with a failed status.
   - Downscale to a long edge of `AI_MAX_IMAGE_EDGE` (1500), JPEG quality 85 (AC-22).
4. **Call.**
   - **Budget re-check:** the worker re-checks the budget first. If it is reached, the capture becomes `failed` with error "monthly budget reached" and stays retryable. No provider call is made and no `ai_calls` row is written.
   - The provider is called outside any transaction (`AI_TIMEOUT_SECONDS`=60).
   - **Immediately after the call returns or raises**, the worker inserts an `ai_calls` row with the usage and the outcome, before validation or apply.
   - **Known undercount:** a timeout records 0 usage because the provider returns no usage, although it may still bill for the call. This is documented in operations.md and on the settings page ("timeouts not counted").
5. **Apply** (`apply.py`, one `write_tx`).
   - **Location resolution:** the target is `capture.location_id`. If that location was soft-deleted meanwhile, the worker uses the nearest live ancestor (falling back to Inbox) and sets `needs_review=1`. A reconcile against a deleted location fails with the error "location removed".
   - **Shelf:** items go to the target location, with `needs_review = confidence < AI_CONFIDENCE_THRESHOLD` (0.7) (AC-13, AC-14).
   - **Receipt:** items go to Inbox with `needs_review=1`, price and purchase date. The capture stores `merchant`. `purchase_date` fallback order: the line value, then `ReceiptMeta.purchase_date`, then the capture's `created_at` date. A denylist regex (`tax|total|subtotal|bag|deposit|change|tip`) acts as a second filter for non-product lines (AC-17).
   - **Text:** each `location_hint` is matched to a live location by case-insensitive exact name, then by unique prefix. With no match, the item goes to the capture's location or Inbox with `needs_review=1` (AC-18).
   - All items link to `photo_id` (AC-16), and `search.reindex_items` runs in the same transaction.
6. **Reconcile** (`reconcile.py`, AC-20).
   - A `matched_existing_id` that is not in the request's `existing_items` is treated as unmatched, so a new item is inserted instead.
   - Unmatched results become new items.
   - A matched item gets `last_seen_at`. If its quantity differs, a `quantity` proposal with `proposed_quantity` is created; quantities never change automatically.
   - Unseen direct items get a `missing` proposal.
   - The one-open-proposal-per-item index is respected with the exact form `INSERT INTO review_proposals(type,item_id,capture_id,proposed_quantity,status,created_at) VALUES (...,'open',...) ON CONFLICT(item_id) WHERE status='open' DO NOTHING`. The conflict target repeats the partial index's WHERE clause, which SQLite requires in order to match a partial unique index. An integration test inserts twice and asserts one row.
7. **Failure.**
   - A transient error re-queues the capture once with `status='queued', not_before=now+5s`, counted against attempts. The worker never sleeps.
   - A permanent error, invalid output, or exhausted attempts marks the capture `failed` with `error`. The input is kept, and Retry (`POST /captures/{id}/retry`) resets it to `queued` with `attempts=0` (AC-25).

### 3.7 Auth, sessions, proxy, CSRF
- **Password (AC-28).**
  - Stored as an argon2id hash in `settings`.
  - If `HOUSEHOLD_PASSWORD` is set, it is hashed at startup and replaces the stored hash when it differs. **The env var overrides `cli set-password` on every restart** (documented in operations.md).
  - Without the env var or a stored hash, `/setup` is enabled, and every route redirects there.
  - `/setup` requires a **one-time setup token**: 32 random bytes generated at startup, printed to the container logs, and required as a form field (constant-time compare). After a successful setup the token is discarded, and `/setup` returns 404.
- **Sessions (AC-29).**
  - Cookie `inv_session`: 32 random bytes, HttpOnly, SameSite=Lax, Path=/. The DB stores the token's sha256.
  - Expiry is 30 days (`SESSION_DAYS`), refreshed when less than 15 days remain.
  - Logout deletes the session row. **Any password change** (setup, env override, `cli set-password`) runs `DELETE FROM sessions`. v1 has **no** settings-page password form; that is an ADR follow-up.
- **Access control (AC-27).**
  - Allowlist: `/login`, `/setup`, `/static/*`, `/healthz`.
  - Unauthenticated `/api/*` requests get 401 JSON. Unauthenticated htmx requests get 401 with `HX-Redirect`. Pages get 303 to `/login?next=<path>`, where `next` must start with `/` and not `//` (AC-10).
  - **All redirects use relative `Location` headers** (such as `/login?next=...`), so a misconfigured Host or scheme can never produce an absolute redirect to the wrong origin.
  - Two authenticated JSON routes exist to test against: `GET /api/export.json` and `GET /api/items/{id}`.
- **Throttle (AC-30).**
  - In-memory: 5 failures per 15 min per client IP returns 429 with Retry-After.
  - **Global progressive delay:** once global failures across all IPs exceed 50 in 15 min, each further login attempt, from any IP and whether right or wrong, is delayed by `min(2s × (failures − 50), 30s)` before the password is checked. It never hard-locks, so the household can always log in, just slowly during an attack. This blunts distributed or XFF-spoofed guessing.
  - The delay is served with `asyncio.sleep`, so it does not block other requests.
  - Counters reset on restart (accepted risk).
- **Proxy (AC-31).**
  - `docker-entrypoint.sh` execs `uvicorn ... --proxy-headers`. Trusted proxies come from uvicorn's native `FORWARDED_ALLOW_IPS` env var, because exec-form CMD does not expand variables. The default in the compose file and `.env.example` is `127.0.0.1,172.16.0.0/12`, which covers the Docker bridge networks.
  - Step 2 verifies that the pinned uvicorn accepts CIDR: a test sends a request from a 172.x client and asserts the forwarded scheme and IP are honored, while a request from 10.x is not.
  - **Port exposure (compose):** use `expose: ["8000"]` when the reverse proxy shares a Docker network with the app. Otherwise publish `127.0.0.1:8000:8000`, never `0.0.0.0`.
  - operations.md warns that trusting `172.16.0.0/12` is **unsafe** with Docker's userland-proxy, hairpin NAT, rootless Docker or Podman. In those setups, every client appears to come from a bridge address and could spoof `X-Forwarded-For`. Set `FORWARDED_ALLOW_IPS` to the proxy's exact IP instead.
  - **`COOKIE_SECURE`:** `auto` sets the Secure flag from the **effective request scheme after trusted-proxy handling**, so plain-HTTP LAN access such as `http://nas:8000` still gets a working cookie. `true` forces the Secure flag always, and `false` never sets it.
- **CSRF (Django-style, `csrf.py`).** Every POST, PUT, PATCH or DELETE must pass three checks; otherwise it returns **403**:
  - `Origin` (or, if absent, `Referer`) must match the effective request Host after proxy handling, the `PUBLIC_BASE_URL` host, or an entry in the optional `ALLOWED_HOSTS` (comma-separated). Both sides are compared as normalized `(host, port)` pairs: lowercase host, with port 80 for http and 443 for https when no port is given. So `https://inv.example.com` matches Host `inv.example.com:443`.
  - htmx mutation endpoints also require the `HX-Request: true` header.
  - No state changes happen over GET.
  - The e2e server sets `PUBLIC_BASE_URL=http://127.0.0.1:<port>` so that Origin matches.

### 3.8 Config (`app/config.py`, `.env.example`)
| Var | Default | Notes |
|---|---|---|
| `PUBLIC_BASE_URL` | required; `.env.example` ships `http://localhost:8000` | `scheme://host[:port]`, no path. Startup error if invalid. Change it before printing tags. |
| `DATA_DIR` | `/data` | Holds `inventory.db`, `photos/`, `backups/` |
| `PUID` / `PGID` | `1000` / `1000` | Used by the entrypoint only when it starts as root (see Step 1) |
| `HOUSEHOLD_PASSWORD` | unset | Overrides the stored hash on each start |
| `AI_PROVIDER` / `AI_API_KEY` | `anthropic` / unset | Without a key, AI capture is disabled with a message and manual entry works |
| `AI_MODEL` | a current vision-capable Claude Sonnet-class ID written in `.env.example` (checked against Anthropic docs at implementation time) | |
| `AI_INPUT_PRICE_PER_MTOK` / `AI_OUTPUT_PRICE_PER_MTOK` | unset | USD. Required when `MONTHLY_AI_BUDGET` is set |
| `MONTHLY_AI_BUDGET` | unset | USD. Stop when `SUM(ai_calls.cost_micros)` for the month is at or above the budget |
| `TZ` | `UTC` | `ai_calls.started_at` is stored in UTC; month boundaries are computed in TZ and converted to a UTC range |
| `AI_CONFIDENCE_THRESHOLD`, `AI_TIMEOUT_SECONDS`, `AI_MAX_IMAGE_EDGE`, `MAX_UPLOAD_MB` | `0.7`, `60`, `1500`, `25` | |
| `SESSION_DAYS`, `COOKIE_SECURE`, `FORWARDED_ALLOW_IPS`, `ALLOWED_HOSTS` | `30`, `auto`, `127.0.0.1,172.16.0.0/12`, unset | |
| `WORKER_AUTOSTART` | `true` | Tests set it to `false` and call `worker.run_once()` |
| `BACKUP_INTERVAL_HOURS` | `24` | The worker writes `backups/latest.db` |

## 4. Implementation Steps

### Step 1: skeleton, config, database, container
**Files:** `pyproject.toml`, `uv.lock`, `app/main.py`, `app/config.py`, `app/db/{connection,migrate}.py`, `app/db/migrations/{0001_init.sql,README}`, `app/inventory/shortcode.py`, `Dockerfile`, `docker-entrypoint.sh`, `docker-compose.yml`, `.env.example`, `.dockerignore`, `tests/conftest.py`, `tests/unit/test_shortcode.py`, `tests/integration/{test_migrations,test_code_immutability,test_write_tx}.py`.

**Build:**
- The schema from 3.3, including the triggers, the FTS table, and the `ensure_inbox()` hook.
- `connect()` and `write_tx()`.
- `/healthz`.
- **Dockerfile:** has **no `USER` line**. The container starts as root only so the entrypoint can fix ownership; the app itself never runs as root.
- **`docker-entrypoint.sh`** (POSIX sh):
  - Runs `mkdir -p "$DATA_DIR"/photos "$DATA_DIR"/backups`.
  - **If `id -u` is 0:** it runs `find "$DATA_DIR" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} +`, which fixes only mis-owned files and never runs a blanket `chown -R`. If that chown fails (for example NFS or CIFS root_squash), it logs a warning and continues. It then runs `exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups uvicorn ...`. setpriv is from util-linux, which bookworm-slim already includes.
  - **If already non-root** (compose `user:`, rootless Docker, Podman): it skips the chown and the privilege drop and runs `exec uvicorn ...` directly.
- **Compose:** `expose: ["8000"]` for a shared proxy network, with a commented `ports: ["127.0.0.1:8000:8000"]` alternative, and `stop_grace_period: 15s`.

**Done when:**
- `cp .env.example .env && docker compose up`, using the shipped `PUBLIC_BASE_URL=http://localhost:8000` and the port published on 127.0.0.1, gives `/healthz` 200 and a working login over plain HTTP with a root-created `./data` bind mount.
- The same works with compose `user: "1000:1000"`, where the entrypoint skips the chown.
- Tests pass for all of the following:
  - `UPDATE short_code`, `DELETE` and `INSERT OR REPLACE` on locations all abort.
  - An UPDATE that sets the same code value is allowed.
  - Migrations are idempotent.
  - Both triggers are present in `sqlite_master` after the latest migration.
  - The grep test for REPLACE on locations passes.

**ACs:** AC-1 (schema), AC-9 (DB guard), AC-33 (partial).

### Step 2: auth, proxy, CSRF
**Files:** `app/auth/*`, `app/api/routes.py` (the stub `GET /api/items/{id}`), `app/templates/{login,setup}.html`, `app/cli.py` (set-password), `tests/integration/{test_auth,test_proxy,test_csrf,test_setup_token}.py`.

**Build:** everything in 3.7, plus a check that the pinned uvicorn supports CIDR.

**Done when:**
- A cross-origin POST returns 403.
- An Origin of `https://h` matches Host `h:443` (default-port normalization).
- Every redirect `Location` header is relative.
- `/setup` without the token returns 403.
- A password change invalidates existing sessions.
- The 51st global failure is delayed about 2s (fake clock) but a correct password still logs in.
- `COOKIE_SECURE=auto` omits Secure over plain HTTP and sets it for a forwarded https request from a trusted proxy.
- Login with `next=/l/XXXXXX` lands on that URL.

**ACs:** AC-10 (redirect), AC-27..AC-31.

### Step 3: inventory core (locations, items, search, browse, QR)
**Files:** `app/inventory/{locations,items,categories,search,routes}.py`, `app/qr.py`, templates `home, search, location, location_removed, item_form, label` plus `partials/{item_row,action_sheet,search_results}`, `app/static/{app.css,app.js}`, `tests/unit/{test_locations_tree,test_fts_query}.py`, `tests/integration/{test_items,test_search,test_filters,test_location_urls,test_qr,test_concurrency}.py`.

**Build:**
- **Locations:** CRUD with the cycle guard, delete confirmation with counts, and re-parenting.
- **Location page:** breadcrumb, children, direct items, subtree toggle, URL with a copy button, QR with PNG and SVG download, print label, and Capture and Reconcile buttons.
- **Manual item form:** covers every field (AC-8).
- **Search:** `hx-get="/search" hx-trigger="input changed delay:250ms"`, up to 50 results, each with its full path.
- **Filters:** combinable: `loc&sub=1`, `category`, `kind`, `review=1`, `expiring_days=N` (`expiry_date <= today+N`).
- **Quick actions:** inline `+1` and `−1` (the minus button is disabled at 0). The `⋯` sheet offers "Used up" and "Move to", listing the 5 most recent locations plus the parent and siblings, and a search field. It also has a **"Scan destination tag"** button: this sets `session.pending_move = {item_ids, expires_at: now+10min}` and shows a hint to scan the container. The `GET /l/<code>` handler applies a non-expired pending move to that location inside `write_tx`, clears the pending move, reindexes FTS, and renders the location with an undo toast. An expired or cancelled pending move does nothing. Scanning a removed location's code (410) leaves the pending move in place and says so. No in-page camera or NFC reading is used, because Web NFC is unavailable on iOS; scanning relies on the phone's native camera or NFC opening the URL.

**Done when:**
- `test_concurrency` passes: the worker applies a 12-item capture while 50 concurrent `+1` POSTs run, with zero `SQLITE_BUSY` errors and an exact final quantity.
- `test_search::test_fts_consistency` asserts that `items_fts` matches a from-scratch recomputation after each of these steps: item edit, location rename, subtree move, category rename, category merge, "Used up", and "Used up" undo.
- `test_search::test_subtree_reindex_perf`: moving a subtree containing 2,000 items, including the reindex, completes in under 300ms.
- `test_location_urls::test_scan_to_move`: pending move → `GET /l/<code>` moves the item and the undo restores it. An expired pending move (fake clock at 11 min) does nothing. A pending move plus a 410 location leaves the move pending. A logged-out scan goes through login, and the move applies after the redirect back.

**ACs:** AC-1..AC-6, AC-8..AC-12, AC-32 (layout).

### Step 4: AI provider layer, images, usage ledger, budget
**Files:** `app/ai/*`, `app/capture/{images,budget}.py`, `app/settings_info.py` (photo-directory size, computed with `os.scandir` and cached for 10 minutes), `docs/ai-provider-contract.md`, `app/templates/settings.html`, `tests/unit/{test_images,test_budget,test_fake_provider,test_provider_contract,test_anthropic_adapter_mapping,test_no_sdk_imports}.py`, `tests/fixtures/{shelf.jpg,receipt.jpg,receipt.pdf,iphone.heic,huge.png}`.

**Build:**
- Shared contract tests that run against `FakeProvider` and against the Anthropic adapter over `httpx.MockTransport`.
- A settings page showing the month's tokens and USD cost from `ai_calls`, the budget and remaining amount, the provider and model, `PUBLIC_BASE_URL` with its orphaning warning, the last backup time, the photo-directory size, and a "timeouts are not counted" note.
- Image tests: a decompression bomb is rejected, and HEIC produces a display JPEG.

**ACs:** AC-21..AC-24, AC-26.

### Step 5: capture flows, reconcile, pending and retry, review queue
**Files:** `app/capture/{service,worker,apply,reconcile,routes}.py` (`worker.run_once()` and `WORKER_AUTOSTART`), templates `captures.html`, `review.html`, `partials/capture_status.html`, `app/static/voice.js`, `tests/fixtures/fake_script.json`, `tests/integration/test_capture_{photo,receipt,text,reconcile,failure,budget,poison,deleted_location}.py`, `tests/integration/test_review_queue.py`.

**Build:**
- **Capture entry points:** from the location page (photo, reconcile, text) and from home (photo with a location picker defaulting to the last used location, plus receipt and text).
- **Inputs:** the camera uses `<input type="file" accept="image/*" capture="environment">`. The receipt input uses `accept="image/*,application/pdf"` with no `capture` attribute.
- **Voice:** `voice.js` uses `SpeechRecognition`/`webkitSpeechRecognition` to fill the textarea and hides the mic when unsupported. Only text is sent (AC-19).
- **Review queue `/review`:** each action is one `hx-post` that swaps the row out (AC-7):
  - failed captures (Retry);
  - needs_review items (Keep, Edit, Delete);
  - `missing` proposals (Confirm removal, Still here);
  - `quantity` proposals (Accept N, Dismiss).

**Done when:**
- A poison capture ends as `failed` after 3 attempts.
- The ledger counts billed invalid outputs.
- A transient requeue is not claimed before `not_before` (fake clock).
- The worker's budget re-check failure leaves the capture `failed`/"monthly budget reached" with no `ai_calls` row.
- Inserting a duplicate open proposal yields one row.
- The status endpoint returns 286 on done.

**ACs:** AC-7, AC-13..AC-20, AC-25.

### Step 6: mobile polish, e2e, export, backups, operations docs
**Files:** `app/api/routes.py` (`GET /api/export.json`), `app/cli.py` (`export`, `backup`, `check`), `app/capture/worker.py` (daily backup tick), `tests/integration/{test_export,test_backup}.py`, `tests/e2e/{conftest,test_mobile_flows,test_qr_deeplink,test_capture_e2e}.py`, `tests/scripts/restore_check.sh`, `docs/operations.md`, `README.md`.

**Build:**
- **Touch targets:** at least 44px, with a sticky bottom bar (Search, Capture, Review).
- **Export:** `GET /api/export.json` and `python -m app.cli export` return locations (with codes and deleted flags), items, and photo paths with sha256.
- **Backups:** every `BACKUP_INTERVAL_HOURS`, the worker deletes any stale `backups/latest.db.tmp` (VACUUM INTO fails if the target exists), runs `VACUUM INTO backups/latest.db.tmp`, then atomically renames it to `backups/latest.db`. `cli backup` does the same on demand.
- **Restore docs:**
  - Copy the whole `/data` directory.
  - On restore, run `PRAGMA integrity_check` on `inventory.db`. If it fails, replace it with `backups/latest.db`, deleting the `-wal` and `-shm` files first.
  - `cli check` automates this check.
- **Proxy examples:** Caddy, Traefik and Tailscale serve, with the `FORWARDED_ALLOW_IPS` guidance.
- **`restore_check.sh`** uses the compose override `tests/scripts/compose.restore.yml`. The override sets:
  - `AI_PROVIDER=fake`;
  - `FAKE_AI_SCRIPT=/fixtures/fake_script.json`, with `tests/fixtures/fake_script.json` mounted read-only at `/fixtures/fake_script.json`;
  - `HOUSEHOLD_PASSWORD=restore-test`;
  - `PUBLIC_BASE_URL=http://127.0.0.1:18000`, with the port published as `127.0.0.1:18000:8000` so it matches the curl `Origin` header;
  - `DATA_DIR` bound to a temp directory.

  It runs these stages:
  1. Starts a container.
  2. Seeds it through form POSTs, using `curl` with a session and matching Origin (2 locations, 5 items, 1 photo capture via the fake provider).
  3. Saves the export.
  4. Stops the container and copies `./data` to a temp directory.
  5. Starts a second container on the copy.
  6. Diffs a second export against the first.

**ACs:** AC-15 (a manual real-key run), AC-32, AC-33.

### Traceability
| AC | Step(s) | AC | Step(s) | AC | Step(s) |
|---|---|---|---|---|---|
| AC-1 | 1, 3 | AC-12 | 3 | AC-23 | 4 |
| AC-2 | 3 | AC-13 | 5 | AC-24 | 4, 5 |
| AC-3 | 3 | AC-14 | 5 | AC-25 | 5 |
| AC-4 | 3 | AC-15 | 5, 6 | AC-26 | 4 |
| AC-5 | 3 | AC-16 | 5 | AC-27 | 2 |
| AC-6 | 3 | AC-17 | 4, 5 | AC-28 | 2 |
| AC-7 | 5 | AC-18 | 5 | AC-29 | 2 |
| AC-8 | 3 | AC-19 | 5 | AC-30 | 2 |
| AC-9 | 1, 3 | AC-20 | 5 | AC-31 | 2 |
| AC-10 | 2, 3 | AC-21 | 4 | AC-32 | 3, 5, 6 |
| AC-11 | 3 | AC-22 | 4 | AC-33 | 1, 6 |

## 5. Acceptance Criteria (refined; spec numbering kept)
- **AC-1:** `items` has every listed field, `kind` is CHECK-constrained, `quantity` is an INTEGER ≥ 0, and the manual form round-trips every field.
- **AC-2:** Depth-8 tree. Create, rename, move and delete all work. Moving a location under its own descendant returns 400 and leaves the tree unchanged. Deleting a location re-parents its contents after a counts confirmation, and children keep their codes.
- **AC-3:** With 5,000 seeded items, `GET /search?q=batt` returns "AA batteries" with the path "Garage › Shelf 2 › Blue box". Server p95 is under 150ms. The inputs `"`, `AND` and `*(` return 200 with no error.
- **AC-4:** Each filter alone, and all five combined, returns exactly the expected ids. `sub=1` includes descendants at depth 3 and deeper.
- **AC-5:** The location page lists direct items and child locations, and the subtree toggle lists all descendant items.
- **AC-6:** `+1` and `−1` take 1 tap each, and `−1` stops at 0 with the item kept. "Used up" takes 2 taps. Move to a recent, parent or sibling location takes 2 taps. Other destinations use the action-sheet search, or scan-to-move: 2 taps plus the scan, then the move applies on arrival with undo (see Interpretations). The e2e tests count the clicks, and scan-to-move is simulated in e2e by navigating to `/l/<code>`.
- **AC-7:** Keep, Delete, Confirm removal, Still here, Accept quantity and Dismiss are each one `hx-post` that removes the row. Edit is one tap to the form.
- **AC-8:** The manual form covers every AC-1 field and works while AI is disabled or over budget.
- **AC-9:** A 6-character code is assigned on insert. It resolves after rename, move, restart and all migrations. The DB rejects UPDATE, DELETE and REPLACE of codes, and the triggers are verified present after the latest migration.
- **AC-10:** Logged out, `/l/CODE` → 303 `/login?next=/l/CODE` → after login, `/l/CODE`. Logged in, the page shows the Capture and Reconcile buttons.
- **AC-11:** The displayed URL equals `PUBLIC_BASE_URL/l/CODE` even when the request Host differs. The QR PNG decoded with zxing-cpp equals that exact string. The PNG download and print label work.
- **AC-12:** A deleted location's URL returns 410 with "This location was removed" and never shows another location.
- **AC-13:** A capture from `/l/CODE` places its items there. If the location was deleted mid-queue, items go to the nearest live ancestor or Inbox with `needs_review=1`.
- **AC-14:** Each item has name, quantity, category, kind and confidence, with `needs_review = confidence < threshold`. Items are saved without a confirm step.
- **AC-15:** Pipeline overhead is under 2s (fake provider, 12 items). With a real key and an empty queue, p50 is 20s or less over 5 runs, recorded in `docs/operations.md`. The queue position is shown when captures wait.
- **AC-16:** The stored original's sha256 equals the upload's, and the items' `photo_id` points to it. HEIC also gets a display JPEG.
- **AC-17:** The JPEG and PDF receipt fixtures produce only product lines, placed in Inbox with price_cents, purchase_date (by the fallback order) and needs_review=1. `merchant` is stored.
- **AC-18:** With a Garage location present, the sentence yields milk ×2 (in the current location or Inbox) and a drill in Garage.
- **AC-19:** The mic fills the textarea (e2e with a stubbed SpeechRecognition). The network log shows only the text POST.
- **AC-20:** New items are added automatically. Unseen items produce `missing` proposals, and changed counts produce `quantity` proposals. Nothing is removed or decremented without a tap. There is at most one open proposal per item, and invalid `matched_existing_id` values are ignored.
- **AC-21:** No module outside `app/ai/` imports `anthropic` (test). Capture code uses only `provider` and `registry`.
- **AC-22:** Every image the FakeProvider records has a long edge of 1500 or less, for JPEG, HEIC and PDF inputs.
- **AC-23:** Each provider call writes an `ai_calls` row, including failures that still reported usage. Settings shows the month's SUM in USD, which equals the ledger computed in TZ.
- **AC-24:** When the month total is at or above the budget, the capture endpoints return 409 with the message, the buttons are disabled, and manual add returns 200. Overshoot is at most one call (single-worker invariant).
- **AC-25:** A timeout or exception leaves the capture `failed` with its input preserved, and Retry succeeds. This survives a container restart. A poison input stops after 3 attempts.
- **AC-26:** `docs/ai-provider-contract.md` exists, and the shared contract tests pass for the fake and for the mocked Anthropic adapter.
- **AC-27:** A parametrized test over all routes except the allowlist: pages return 303 to login, and `/api/export.json`, `/api/items/1` and htmx requests return 401.
- **AC-28:** The password can be set by env or by `/setup` with the log token. Only an `$argon2id$` hash is stored. `/setup` returns 404 afterwards, and a password change clears all sessions.
- **AC-29:** Max-Age is about 30 days. A session is valid on day 29 and invalid on day 31 (test clock).
- **AC-30:** The 6th failure in 15 minutes from one IP returns 429, and another IP is unaffected. Once global failures exceed 50 in 15 minutes, every attempt is delayed by about 2s per extra failure (capped at 30s), but a correct password always succeeds, so there is no lockout.
- **AC-31:** Forwarded headers are honored only from `FORWARDED_ALLOW_IPS` (the CIDR test). With `COOKIE_SECURE=auto`, cookies are Secure when the effective scheme after trusted-proxy handling is https, and not Secure on plain-HTTP LAN access. A cross-origin POST returns 403, including when the Origin port is written out explicitly. All redirects are relative.
- **AC-32:** At a 390×844 WebKit viewport, search, `+1`/`−1` and capture are in the bottom 60% of the screen, touch targets are at least 44px, and the input has `capture="environment"`.
- **AC-33:** `cp .env.example .env` plus `docker compose up`, with the shipped `PUBLIC_BASE_URL=http://localhost:8000`, gives a working login on a root-owned bind mount. It also works with compose `user:`. `restore_check.sh` shows an identical export from the copied `./data`, and `backups/latest.db` exists after one backup tick.

## 6. Risks and Mitigations
| Risk | Mitigation |
|---|---|
| AI invents items or miscounts | Confidence threshold flags low-confidence items for review. Receipts always need review. Removals and quantity changes are proposals. Photos are linked to items for checking. |
| Cost overrun | 1500px downscale, a 3-page PDF cap, and a budget check before enqueue and before each call. The ledger includes failed but billed calls. The single worker bounds overshoot to one call. |
| Malformed provider output | Forced tool call plus Pydantic validation. Usage is still logged, and the capture stays retryable. |
| iOS Safari camera and HTTPS | The file-input `capture` attribute needs no secure context. Web Speech needs HTTPS, and the mic is hidden when unsupported (keyboard dictation still works). WebKit e2e plus one real iPhone check. |
| Short code altered or reused | Triggers with a WHEN guard, `recursive_triggers`, a REPLACE ban, a no-rebuild migration rule, and a trigger-presence test after every migration. |
| SQLite contention between worker and web | `BEGIN IMMEDIATE` for every write, WAL, busy_timeout 5000, provider calls made outside transactions, and the 50-POST concurrency test. |
| Wrong `PUBLIC_BASE_URL`, so bad tags get printed | Required at startup with no path allowed. The label shows the URL text. Settings warns that changing it orphans printed tags. |
| Setup takeover on a fresh internet-exposed instance | `/setup` needs the one-time token from the container logs. `HOUSEHOLD_PASSWORD` skips setup entirely. |
| Proxy header spoofing | The trusted list defaults to loopback plus the Docker bridge, not `*`. Compose uses `expose` or a `127.0.0.1:` publish. operations.md flags the userland-proxy, hairpin, rootless and Podman cases where the bridge CIDR is unsafe. The global progressive delay limits damage. |
| Global throttle trade-off: availability vs brute force | A hard global lock would let an attacker lock the household out, so v1 uses a progressive delay (about 2s per failure over 50 in 15 min, capped at 30s) that never locks. The cost is that a distributed attacker still gets about 1 guess per 30s once the delay is at its cap. That is negligible against an argon2-hashed password chosen by the household. Documented, including a recommendation to use a long passphrase. |
| Throttle resets on restart | Accepted, since restarts are rare and argon2 slows guessing. Documented. |
| Worker killed mid-call by `docker stop` (10s grace vs 60s timeout) | The capture stays `processing`, is re-queued on startup, and its input is on disk. At most one call's usage may go unrecorded. |
| Timeout usage undercount | A timeout records 0 tokens although the provider may bill. Documented, and Settings says so. The budget may be overshot by that amount. |
| Bind mount on NFS/CIFS with root_squash | The entrypoint's chown failure is only a warning. Users on such mounts set compose `user:` to the share's owner. |
| Unbounded photo disk growth | `MAX_UPLOAD_MB`, and Settings shows the photo directory size. Pruning photos whose items are all deleted is a follow-up. |
| Backup taken mid-write | A daily `latest.db` via `VACUUM INTO` means a naive copy always holds a consistent snapshot. The documented restore uses `integrity_check` and falls back to `latest.db`. |
| Poison input crash loop | Attempts are incremented on claim and capped at 3. `MAX_IMAGE_PIXELS` is set and PDFs have size and page caps. |
| Bind mount owned by root | When started as root, the entrypoint chowns only mis-owned files to PUID/PGID and drops privileges with `setpriv`. When already non-root, it skips both steps. |

## 7. Verification Steps
- **Unit:** shortcode generation and normalization, tree cycle guard, FTS query escaping (fuzz), image downscale and limits, budget month window in TZ, cost math, receipt denylist and date fallback, location-hint matching, FakeProvider behavior.
- **Integration:** FastAPI `TestClient` against a temp `DATA_DIR` with real SQLite, `AI_PROVIDER=fake` and `WORKER_AUTOSTART=false`. Tests call `worker.run_once()` directly so results are deterministic. Coverage:
  - auth, setup token, CSRF 403, proxy CIDR;
  - every capture type, including poison inputs and deleted locations;
  - ledger and budget;
  - reconcile proposals and the review queue;
  - search at 5k items, filters, concurrency;
  - export and backup.
- **Provider faking:** `app/ai/fake.py` is used by unit, integration and e2e tests through `FAKE_AI_SCRIPT` (`tests/fixtures/fake_script.json`). It returns scripted results, raises each error class with or without usage, or sleeps past the timeout, and it records every request. The Anthropic adapter is tested only through `httpx.MockTransport` with recorded JSON, so no test touches the network.
- **E2E (Playwright, Chromium and WebKit, iPhone 13 viewport):**
  - The server runs with `AI_PROVIDER=fake`, `FAKE_AI_SCRIPT=tests/fixtures/fake_script.json`, `PUBLIC_BASE_URL=http://127.0.0.1:<port>` and `HOUSEHOLD_PASSWORD=test`.
  - Flows covered: QR deep link through login, capture upload with polling that stops on 286, quick-action tap counts, voice stub, review queue.
- **Planned commands:**
  - `uv sync`, then `uv run ruff check . && uv run ruff format --check .`
  - `uv run pytest tests/unit tests/integration -q`
  - `uv run playwright install --with-deps chromium webkit && uv run pytest tests/e2e -q`
  - `cp .env.example .env && docker compose build && docker compose up -d && curl -fsS localhost:8000/healthz`
  - `bash tests/scripts/restore_check.sh` (AC-33)
  - Manual checks:
    - 5 real-key shelf photos to time AC-15;
    - a real iPhone opening the rear camera over HTTPS through the proxy (AC-32);
    - a printed QR scanned by a phone (AC-11).
- **By AC group:**
  - Core, AC-1..AC-12: integration tests, plus e2e for AC-6 and AC-10.
  - Capture, AC-13..AC-26: integration with the fake provider plus the contract tests; AC-15 also manual.
  - Household, AC-27..AC-33: integration, e2e and the docker restore script.
- **Completion gate:** all tests green. No `skip`, `only` or TODO stubs. Every AC maps to a passing test id or to a manual record in `docs/operations.md`.

## 8. ADR: Stack and core invariants
- **Decision:** a single-process FastAPI app with server-rendered Jinja2 and htmx, on raw SQLite (WAL, `BEGIN IMMEDIATE` writes, DB-enforced immutable short codes, an FTS5 table that stores its own content and is re-indexed by the app). One durable in-process capture worker calls AI through a narrow provider interface; the only v1 adapter is Anthropic.
- **Drivers:** operational simplicity; implementation speed and maintenance; image, PDF and AI tooling with full SQLite control.
- **Alternatives considered:** A (SvelteKit and Node: needs a build step, weak PDF rasterizing); B (FastAPI plus an SPA: double the surface area); D (Django 5 and htmx: strongest alternative, rejected because SQLite table-rebuild migrations drop the short-code triggers and FTS5 would sit outside the ORM).
- **Why chosen:** C vs D is close, and choosing between them is a judgment call.
  - D's trigger loss on table-rebuild migrations is mitigable with a `post_migrate` `CREATE TRIGGER IF NOT EXISTS` plus a presence test.
  - Django 5.1+ offers `transaction_mode="IMMEDIATE"`.
  - C needs the same never-rebuild discipline anyway.

  The deciding factors are direct control of the SQL, transactions and FTS5, plus a smaller dependency surface. Against those stands D's framework-vetted CSRF, sessions and host handling, which C offsets by copying Django's patterns and testing them explicitly.
- **Consequences:**
  - CSRF, session, proxy and throttle code is hand-written, so it needs dedicated tests.
  - One worker serializes captures, which puts a queue caveat on AC-15.
  - Migrations are hand-written SQL and must follow the no-rebuild rule.
  - The in-memory throttle resets on restart.
- **Follow-ups:**
  - An Ollama adapter.
  - Pruning orphaned photos.
  - A budget reservation step if more workers are ever added.
  - Optional per-location capture concurrency.
  - A settings-page password form. v1 has none; `cli set-password` and `HOUSEHOLD_PASSWORD` cover it.
  - Estimating the usage of timed-out calls.

## Changelog (iteration 2)
Architect verdict: APPROVE WITH CHANGES. Critic verdict: REJECT-REVISE. Both keep Option C. Changes applied:
1. **FTS:** changed to a regular FTS5 table that stores its own content, re-indexed in the app by `search.reindex_items` within the write transaction. Soft-deleted items are removed from it, and user tokens are quoted as prefix phrases.
2. **Transactions:** `isolation_level=None` plus a `write_tx()` BEGIN IMMEDIATE helper for every write, and a concurrency test (worker plus 50 `+1` POSTs, zero SQLITE_BUSY).
3. **Usage ledger:** `ai_calls` table written right after each call and before validation. The monthly SUM uses UTC storage with the month boundary in TZ, and the single-worker invariant is stated explicitly.
4. **Security:**
   - `/setup` needs a token printed to the logs.
   - `FORWARDED_ALLOW_IPS` defaults to `127.0.0.1,172.16.0.0/12` via uvicorn's env var, with a CIDR test.
   - A global login cap of 50 per 15 min.
   - Django-style Origin/Referer CSRF check, HX-Request on htmx mutations, and a 403 test; the e2e `PUBLIC_BASE_URL` is stated.
   - `COOKIE_SECURE=auto` comes from the `PUBLIC_BASE_URL` scheme.
   - Password changes clear all sessions, and the env-var override is documented.
   - New risks added: setup takeover, header spoofing, disk growth, throttle reset.
5. **Short codes:**
   - Triggers with a WHEN guard, `recursive_triggers=ON`, a REPLACE grep ban, a no-rebuild migration rule, and a trigger-presence test.
   - The Inbox code is created by the `ensure_inbox()` Python hook.
   - `PUBLIC_BASE_URL` rejects a path, and Settings warns that changing it orphans printed tags.
6. **Capture robustness:**
   - Attempts are incremented on claim and capped at 3.
   - `MAX_IMAGE_PIXELS` is set, and PDFs have size and page caps.
   - A deleted target location falls back to the nearest ancestor or Inbox; a reconcile against it fails.
   - HEIC gets a display JPEG.
   - Added python-multipart.
   - The status partial shows the queue position, and polling stops with 286.
   - `merchant` is stored, and the purchase_date fallback is defined.
7. **Reconcile:** quantity changes become `quantity` proposals. There is at most one open proposal per item, and invalid `matched_existing_id` values are ignored.
8. **Backups:** a daily `VACUUM INTO backups/latest.db`, plus a restore procedure using `integrity_check` and a `cli check` command.
9. **Container:** PUID/PGID with an entrypoint chown, `cp .env.example .env` in Step 1's done-when, and `playwright install --with-deps`.
10. **Verification artifacts:** declared the `/api/export.json` and `cli export` endpoint and its test, the form-POST seeding, `restore_check.sh`, `fake_script.json`, `worker.run_once()` and `WORKER_AUTOSTART`, zxing-cpp as a dev dependency (segno-payload fallback removed), and `/api/items/{id}` as a test target for AC-27.
11. **Alternatives:** added Option D (Django 5) with a steelman and its rejection rationale, corrected A's cons (native modules ship arm64 prebuilds), and recorded the synthesis (borrow Django patterns).
12. **Resolved open questions:** INTEGER quantity that floors at 0, re-parenting on delete with children keeping their codes, USD budget.
13. **Interpretations section:** added for the AC-6 two-tap scope and the AC-15 queue latency. Added the ADR section.

## Changelog (final improvements)
Architect and Critic both returned APPROVE WITH IMPROVEMENTS on iteration 2. Applied:
1. **FTS:**
   - `reindex_items` is set-based: one recursive CTE for all paths and a single `INSERT…SELECT` join, with ids passed through a temp table.
   - Reindex now also runs on category rename or merge and on "Used up" undo, and both are covered by the FTS-consistency test.
   - Added a performance test: a 2,000-item subtree move with reindex in under 300ms.
2. **Worker and budget:**
   - `captures.not_before` is filtered by the claim query, and a transient requeue sets it to now+5s.
   - A failed budget re-check in the worker gives `failed`/"monthly budget reached" (retryable) with no `ai_calls` row.
   - The timeout 0-usage undercount is documented.
   - The exact `ON CONFLICT(item_id) WHERE status='open' DO NOTHING` form is specified.
   - A stale `latest.db.tmp` is deleted before `VACUUM INTO`.
3. **Login throttle:** the global hard lock is replaced by a progressive delay (about 2s per failure over 50 in 15 min, capped at 30s, never locking). The per-IP throttle is kept. Added a trade-off risk row and updated AC-30.
4. **CSRF, proxy and cookies:**
   - Default ports 80/443 are normalized in the Origin/Host comparison, with a test.
   - All redirects use relative `Location` headers.
   - Compose uses `expose`, or publishes on `127.0.0.1:8000:8000`.
   - operations.md warns about the bridge CIDR with userland-proxy, hairpin NAT, rootless Docker and Podman.
   - `COOKIE_SECURE=auto` follows the effective request scheme, and `true` forces the flag.
   - `.env.example` ships `PUBLIC_BASE_URL=http://localhost:8000`.
5. **Entrypoint and container:**
   - `setpriv` (util-linux) replaces gosu.
   - chown touches only mis-owned files, via `find`.
   - chown and the privilege drop are skipped when already non-root.
   - A chown failure logs a warning and continues.
   - The Dockerfile has no `USER` line, and Step 1's wording matches.
   - The `docker stop` grace period vs the provider timeout is documented, with `stop_grace_period: 15s`.
   - HEIC derivative generation moved to the worker's prepare step.
6. **restore_check.sh:** uses `tests/scripts/compose.restore.yml`, which sets `AI_PROVIDER=fake`, mounts `fake_script.json` read-only as `FAKE_AI_SCRIPT`, and sets `PUBLIC_BASE_URL` to match the curl Origin.
7. **Consistency fixes:**
   - The password-change contradiction is resolved: no settings form in v1 (ADR follow-up).
   - The photo-directory size display is added to Step 4 (`app/settings_info.py`).
   - The migrations README notes that `recursive_triggers` is per connection, so external sqlite3 shells bypass the REPLACE protection.
8. **ADR and Option D:** now state honestly that C vs D is a close judgment call. D's trigger mitigation (`post_migrate` plus a presence test) and Django 5.1+ IMMEDIATE transactions are acknowledged. The deciding factors are SQL/FTS control and dependency surface vs framework-vetted security. The decision stays C.

## Changelog (user decisions, 2026-09-29)
1. AC-6: the user accepted the 2-tap scope and added **scan-to-move**: a session-held pending move is applied when a location's permanent URL is opened by QR or NFC. The Step 3 build and done-when, and the refined AC-6, are updated.
2. AC-15: the user accepted the single serial worker and its queue latency.
3. Known risk added by the user decision: if the phone's QR scanner opens URLs in a different browser, or outside an installed home-screen web app, the session cookie may be absent. The user then logs in and returns via redirect, and the pending move is lost only if it was stored in the other browser's session. Document this in `docs/operations.md`.
4. Plan approved for execution via ralph.

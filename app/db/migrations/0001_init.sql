-- Initial schema. See README in this directory before writing a new migration:
-- never rebuild or drop `locations`; only ALTER TABLE ... ADD COLUMN.

CREATE TABLE locations (
    id          INTEGER PRIMARY KEY,
    short_code  TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    parent_id   INTEGER NULL REFERENCES locations(id),
    is_system   INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1)),
    deleted_at  TEXT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX locations_parent ON locations(parent_id);

-- Short codes are permanent (printed on QR/NFC tags). Setting the same value is allowed.
CREATE TRIGGER locations_code_immutable
BEFORE UPDATE OF short_code ON locations
WHEN OLD.short_code IS NOT NEW.short_code
BEGIN
    SELECT RAISE(ABORT, 'short_code immutable');
END;

-- Locations are soft-deleted (deleted_at) so their codes are never reused.
-- With PRAGMA recursive_triggers=ON this also fires for REPLACE's conflict deletes.
CREATE TRIGGER locations_no_delete
BEFORE DELETE ON locations
BEGIN
    SELECT RAISE(ABORT, 'locations are soft-deleted');
END;

CREATE TABLE categories (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE captures (
    id           INTEGER PRIMARY KEY,
    type         TEXT NOT NULL CHECK (type IN ('photo', 'receipt', 'text', 'reconcile')),
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'processing', 'done', 'failed')),
    location_id  INTEGER NULL REFERENCES locations(id),
    input_text   TEXT NULL,
    merchant     TEXT NULL,
    error        TEXT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0,
    not_before   TEXT NOT NULL, -- UTC ISO-8601
    provider     TEXT NULL,
    model        TEXT NULL,
    duration_ms  INTEGER NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at  TEXT NULL
);
CREATE INDEX captures_queue ON captures(status, not_before, created_at);

CREATE TABLE photos (
    id                INTEGER PRIMARY KEY,
    capture_id        INTEGER NULL REFERENCES captures(id),
    rel_path          TEXT NOT NULL,
    display_rel_path  TEXT NULL,
    sha256            TEXT NOT NULL,
    width             INTEGER NULL,
    height            INTEGER NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX photos_capture ON photos(capture_id);

CREATE TABLE items (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    quantity       INTEGER NOT NULL DEFAULT 1 CHECK (quantity >= 0),
    category_id    INTEGER NULL REFERENCES categories(id),
    kind           TEXT NOT NULL CHECK (kind IN ('consumable', 'belonging')),
    location_id    INTEGER NOT NULL REFERENCES locations(id),
    expiry_date    TEXT NULL,
    purchase_date  TEXT NULL,
    price_cents    INTEGER NULL,
    notes          TEXT NULL,
    photo_id       INTEGER NULL REFERENCES photos(id),
    capture_id     INTEGER NULL REFERENCES captures(id),
    confidence     REAL NULL,
    needs_review   INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
    last_seen_at   TEXT NULL,
    deleted_at     TEXT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX items_location ON items(location_id);
CREATE INDEX items_category ON items(category_id);

-- Authoritative AI usage ledger: one row per provider call, written right after the call.
CREATE TABLE ai_calls (
    id             INTEGER PRIMARY KEY,
    capture_id     INTEGER NULL REFERENCES captures(id),
    started_at     TEXT NOT NULL, -- UTC ISO-8601
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_micros    INTEGER NOT NULL DEFAULT 0,
    outcome        TEXT NOT NULL CHECK (outcome IN
                   ('ok', 'invalid_output', 'transient_error', 'permanent_error', 'timeout'))
);
CREATE INDEX ai_calls_started ON ai_calls(started_at);

CREATE TABLE review_proposals (
    id                 INTEGER PRIMARY KEY,
    type               TEXT NOT NULL CHECK (type IN ('missing', 'quantity')),
    item_id            INTEGER NOT NULL REFERENCES items(id),
    capture_id         INTEGER NULL REFERENCES captures(id),
    proposed_quantity  INTEGER NULL,
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'accepted', 'dismissed')),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    resolved_at        TEXT NULL
);
-- Insert with: ON CONFLICT(item_id) WHERE status='open' DO NOTHING
CREATE UNIQUE INDEX one_open_proposal ON review_proposals(item_id) WHERE status = 'open';

CREATE TABLE sessions (
    token_hash    TEXT PRIMARY KEY, -- sha256 of the cookie token
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL
);

-- Keys: password_hash, last_backup_at.
CREATE TABLE settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- Regular FTS5 table storing its own content; rowid = items.id.
-- No triggers: search.reindex_items() maintains it inside each write transaction.
CREATE VIRTUAL TABLE items_fts USING fts5(
    name, category, notes, location_path,
    tokenize = 'unicode61 remove_diacritics 2'
);

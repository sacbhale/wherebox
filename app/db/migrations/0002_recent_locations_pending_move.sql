-- Step 3: "recent locations" for the Move-to sheet, and the scan-to-move pending move.
-- locations only ever gets ADD COLUMN (see README).

-- Set when an item is created in or moved into the location; the Move-to sheet lists the
-- most recently used ones.
ALTER TABLE locations ADD COLUMN last_used_at TEXT NULL;

-- JSON {"item_ids": [...], "expires_at": "<UTC ISO>"} while a scan-to-move is pending.
ALTER TABLE sessions ADD COLUMN pending_move TEXT NULL;

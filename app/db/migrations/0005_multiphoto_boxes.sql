-- Multi-photo captures and annotated item boxes (spec-multiphoto.md). CREATE TABLE and
-- ADD COLUMN only (see README).

-- Order of a photo within its capture (submission order, 0-based).
ALTER TABLE photos ADD COLUMN position INTEGER NOT NULL DEFAULT 0;

-- Set on an "add another angle" capture (an extension): the id of its group's root capture.
-- A capture group = a root capture + its extensions (never an extension of an extension).
ALTER TABLE captures ADD COLUMN parent_capture_id INTEGER NULL REFERENCES captures(id);
CREATE INDEX captures_parent ON captures(parent_capture_id);

-- Where an item was seen: up to 4 boxes per item per photo, as fractions (0..1) of the
-- upright display image (photos.display_rel_path, photos.width/height). Annotation data:
-- replacing an item's boxes deletes and re-inserts its rows, only on the photos where the
-- model returned a box for it in that call (omissions keep older annotations).
CREATE TABLE item_boxes (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    photo_id    INTEGER NOT NULL REFERENCES photos(id),
    capture_id  INTEGER NOT NULL REFERENCES captures(id),
    idx         INTEGER NOT NULL CHECK (idx BETWEEN 0 AND 3),
    x0          REAL NOT NULL,
    y0          REAL NOT NULL,
    x1          REAL NOT NULL,
    y1          REAL NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    CHECK (0 <= x0 AND x0 < x1 AND x1 <= 1 AND 0 <= y0 AND y0 < y1 AND y1 <= 1),
    UNIQUE (item_id, photo_id, idx)
);
CREATE INDEX item_boxes_photo ON item_boxes(photo_id);

-- The photos a comparison case sends (JSON list of photo ids, position order), taken from
-- its capture group when the case is created. NULL on older cases: their capture's photos.
ALTER TABLE benchmark_cases ADD COLUMN photo_ids_json TEXT NULL;

-- How many images the call sent (image-aware cost estimates). NULL on older rows.
ALTER TABLE ai_calls ADD COLUMN image_count INTEGER NULL;

-- "Added X new, Y already known" for an "add another angle" extension: Y = the live items at the
-- location it matched (boxed or not, including name safety-net matches; matches to items that are
-- gone from there excluded), stored by its apply. NULL on other captures.
ALTER TABLE captures ADD COLUMN known_count INTEGER NULL;

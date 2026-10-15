-- Location overview photo, user-corrected item boxes and "Delete" (spec-location-photo R12).

-- The photo of a photo/reconcile capture (or extension) that shows the most of the place: the
-- model's overview_image when valid, else the call photo with the most boxes, else the first.
-- Set by apply. NULL on older captures (the location page then picks one at read time).
ALTER TABLE captures ADD COLUMN overview_photo_id INTEGER NULL REFERENCES photos(id);

-- A location photo pinned by the user ("Use as location photo"); NULL = picked automatically
-- from the latest done photo/reconcile capture of the location.
ALTER TABLE locations ADD COLUMN overview_photo_id INTEGER NULL REFERENCES photos(id);

-- 'user': an outline drawn by the user ("Fix outline"); later model boxes never replace it.
ALTER TABLE item_boxes ADD COLUMN source TEXT NOT NULL DEFAULT 'model'
    CHECK (source IN ('model', 'user'));

-- Why a soft-deleted item is gone: 'used_up' or 'deleted' (a wrong detection). NULL on live
-- items and on items used up before this column.
ALTER TABLE items ADD COLUMN deleted_reason TEXT NULL;

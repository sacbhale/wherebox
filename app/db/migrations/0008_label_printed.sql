-- Choose which labels to print (spec-label-selection): when the location's label was last
-- marked as printed on a label sheet; NULL = never printed (pre-selected on /labels).
ALTER TABLE locations ADD COLUMN label_printed_at TEXT NULL;

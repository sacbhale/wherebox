"""The export document shared by ``GET /api/export.json`` and ``python -m app.cli export``.

Everything is ordered by id and no timestamp of the export itself is included, so two
exports of the same data are byte-identical and a restore can be checked with ``diff``.
Soft-deleted locations and used-up items are included with ``deleted: true``.
"""

import sqlite3
from typing import Any

EXPORT_VERSION = 1

LOCATIONS_SQL = """
SELECT id, short_code, name, parent_id, is_system, deleted_at, created_at, updated_at
FROM locations ORDER BY id
"""

ITEMS_SQL = """
SELECT i.id, i.name, i.quantity, c.name AS category, i.kind, i.location_id,
       l.short_code AS location_code, i.expiry_date, i.purchase_date, i.price_cents, i.notes,
       i.photo_id, i.capture_id, i.confidence, i.needs_review, i.last_seen_at, i.deleted_at,
       i.deleted_reason, i.created_at, i.updated_at
FROM items i
JOIN locations l ON l.id = i.location_id
LEFT JOIN categories c ON c.id = i.category_id
ORDER BY i.id
"""

PHOTOS_SQL = """
SELECT id, capture_id, rel_path, display_rel_path, sha256, width, height, created_at
FROM photos ORDER BY id
"""


def _rows(conn: sqlite3.Connection, sql: str, flags: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = []
    for row in conn.execute(sql):
        record = dict(row)
        for key in flags:
            record[key] = bool(record[key])
        if "deleted_at" in record:
            record["deleted"] = record["deleted_at"] is not None
        rows.append(record)
    return rows


def build_export(conn: sqlite3.Connection) -> dict[str, Any]:
    """Locations (with codes and deleted flags), items (all fields) and photos (with sha256).

    The reads run in one transaction so the three lists are a consistent snapshot."""
    conn.execute("BEGIN")
    try:
        return {
            "version": EXPORT_VERSION,
            "locations": _rows(conn, LOCATIONS_SQL, ("is_system",)),
            "items": _rows(conn, ITEMS_SQL, ("needs_review",)),
            "photos": _rows(conn, PHOTOS_SQL, ()),
        }
    finally:
        conn.execute("COMMIT")

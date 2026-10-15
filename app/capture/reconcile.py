"""Reconcile a location against a photo (plan 3.6 step 6, AC-20).

The request lists the location's live direct items (``existing_items``). From the result:
new (unmatched) items are inserted right away; a matched item gets ``last_seen_at`` and, if
the count differs, a ``quantity`` proposal; direct items sent to the model but not seen get a
``missing`` proposal (items that arrived during the call do not); seeing an item again
dismisses its open ``missing`` proposal. Nothing is removed or decremented here: proposals
wait for a tap in the review queue. At most one proposal per item is open (``one_open_proposal``).

``apply_reconcile`` re-reads the direct items in the worker's apply transaction; a matched id
that is not one of them (hallucinated, moved away, used up) is treated as unmatched.
New items get their boxes on the call's photos; a matched item's boxes are replaced per photo
(only on the photos where this call boxed it, ``boxes.replace_boxes``; spec-multiphoto:
reconcile also gets boxes). The proposal rules are unchanged.
"""

import sqlite3
from typing import Any

from app.ai.schemas import ExtractResult
from app.capture import boxes
from app.capture.apply import CaptureFailed, insert_with_boxes, is_uncertain
from app.config import Settings
from app.db.timestamps import NOW
from app.inventory import locations, search

LOCATION_REMOVED = "location removed"


def existing_items(conn: sqlite3.Connection, location_id: int) -> list[sqlite3.Row]:
    """Live items directly in the location (not in child locations)."""
    return conn.execute(
        "SELECT id, name, quantity FROM items WHERE location_id = ? AND deleted_at IS NULL"
        " ORDER BY id",
        (location_id,),
    ).fetchall()


def _live_location(conn: sqlite3.Connection, capture: sqlite3.Row) -> int:
    location_id = capture["location_id"]
    if location_id is None or locations.get(conn, location_id) is None:
        raise CaptureFailed(LOCATION_REMOVED)
    return location_id


def request_extras(conn: sqlite3.Connection, capture: sqlite3.Row) -> dict[str, Any]:
    """ExtractRequest fields; fails the capture (before any provider call) when the location
    was soft-deleted."""
    rows = existing_items(conn, _live_location(conn, capture))
    return {"existing_items": [dict(row) for row in rows]}


def propose(
    conn: sqlite3.Connection,
    kind: str,
    item_id: int,
    capture_id: int | None,
    proposed_quantity: int | None = None,
) -> bool:
    """Open a proposal unless the item already has one. True if a row was inserted.
    Call inside write_tx()."""
    cursor = conn.execute(
        "INSERT INTO review_proposals(type,item_id,capture_id,proposed_quantity,status,created_at)"
        f" VALUES (?,?,?,?,'open',{NOW})"
        " ON CONFLICT(item_id) WHERE status='open' DO NOTHING",
        (kind, item_id, capture_id, proposed_quantity),
    )
    return cursor.rowcount == 1


def apply_reconcile(
    conn: sqlite3.Connection,
    settings: Settings,
    capture: sqlite3.Row,
    result: ExtractResult,
    image_photo_ids: list[int],
    extras: dict[str, Any],
) -> list[int]:
    """Runs inside the worker's write_tx. Returns the ids of the new items."""
    location_id = _live_location(conn, capture)
    existing = {row["id"]: row for row in existing_items(conn, location_id)}
    sent = {entry["id"] for entry in extras["existing_items"]}
    seen: dict[int, int] = {}  # existing item id -> counted quantity
    seen_boxes: dict[int, list[boxes.PhotoBox]] = {}
    new_ids: list[int] = []
    for extracted in result.items:
        matched = extracted.matched_existing_id
        if matched is not None and matched in existing:
            seen[matched] = seen.get(matched, 0) + extracted.quantity
            seen_boxes.setdefault(matched, []).extend(boxes.photo_boxes(extracted, image_photo_ids))
            continue
        new_ids.append(
            insert_with_boxes(
                conn,
                capture,
                extracted,
                image_photo_ids,
                location_id=location_id,
                needs_review=is_uncertain(extracted, settings),
            )
        )
    capture_id = capture["id"]
    for item_id, found in seen_boxes.items():
        boxes.replace_boxes(conn, item_id, capture_id, found)
    for item_id, quantity in seen.items():
        conn.execute(f"UPDATE items SET last_seen_at = {NOW} WHERE id = ?", (item_id,))
        conn.execute(
            f"UPDATE review_proposals SET status = 'dismissed', resolved_at = {NOW}"
            " WHERE item_id = ? AND type = 'missing' AND status = 'open'",
            (item_id,),
        )
        if quantity != existing[item_id]["quantity"]:
            propose(conn, "quantity", item_id, capture_id, quantity)
    for item_id in (existing.keys() & sent) - seen.keys():
        propose(conn, "missing", item_id, capture_id)
    boxes.record_overview(conn, capture_id, result, image_photo_ids)
    search.reindex_items(conn, new_ids)
    return new_ids

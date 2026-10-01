""" "Add another angle": extension captures (spec-multiphoto Part 4, R5-R7).

An extension is a ``photo`` capture with ``parent_capture_id`` = its group's root. The worker
sends every photo of the group (old then new) and, as ``existing_items``:

(i)  the live items directly in the location (the same list as reconcile), and
(ii) the group's items that are now elsewhere or used up, marked ``gone`` ("not here any
     more — do not re-add"), so re-sent old photos never re-create them.

Apply (inside the worker's write_tx) never edits or deletes an existing item:

- unmatched -> a new item, as a normal photo capture (location, needs_review by confidence,
  boxes, photo_id = first photo with a box);
- matched (i) -> its boxes replaced per photo (on the photos where this call boxed it; its
  older boxes elsewhere stay, ``boxes.replace_boxes``), ``last_seen_at`` set, its open
  ``missing`` proposal dismissed, and a ``quantity`` proposal opened only when the model
  counted more (an angle can hide things, so lower counts are ignored);
- matched (ii) -> boxes only (no last_seen_at, no proposal, never inserted);
- safety net: an unmatched item whose normalized name equals the name of a sent live item
  (i) is treated as matched to it, and that item is flagged needs_review (logged). A name
  equal only to a gone item (ii) is inserted as a new item with needs_review (e.g. a
  replacement bought after the old one was used up).

The number of live items matched (i) — boxed or not, safety net included — is stored in
``captures.known_count`` ("Added X new, Y already known").

A matched id that was not sent is treated as unmatched (same rule as reconcile).
"""

import logging
import sqlite3
from typing import Any

from app.ai.schemas import ExtractResult
from app.capture import boxes, groups, reconcile
from app.capture.apply import (
    insert_with_boxes,
    is_uncertain,
    normalize_predictions,
    resolve_location,
)
from app.config import Settings
from app.db.timestamps import NOW
from app.inventory import search

log = logging.getLogger(__name__)


def _normal(name: str) -> str:
    return " ".join(name.split()).casefold()


def gone_items(
    conn: sqlite3.Connection, capture: sqlite3.Row, location_id: int
) -> list[sqlite3.Row]:
    """(ii): items created by, or boxed on the photos of, the group's done captures that are
    used up or no longer directly in ``location_id``."""
    root = capture["parent_capture_id"]
    ids = groups.done_capture_ids(conn, root)
    marks = ",".join("?" * len(ids))
    return conn.execute(
        "SELECT id, name, quantity FROM items"
        " WHERE (deleted_at IS NOT NULL OR location_id != ?) AND ("
        f" capture_id IN ({marks}) OR id IN (SELECT b.item_id FROM item_boxes b"
        f" JOIN photos p ON p.id = b.photo_id WHERE p.capture_id IN ({marks})))"
        " ORDER BY id",
        (location_id, *ids, *ids),
    ).fetchall()


def request_extras(conn: sqlite3.Connection, capture: sqlite3.Row) -> dict[str, Any]:
    """ExtractRequest fields: (i) the location's live direct items, then (ii) the group's
    items that are not here any more (``gone``). A soft-deleted location resolves like
    ``apply.resolve_location`` (nearest live ancestor or the Inbox)."""
    location_id, _moved = resolve_location(conn, capture["location_id"])
    here = [dict(row) | {"gone": False} for row in reconcile.existing_items(conn, location_id)]
    gone = [dict(row) | {"gone": True} for row in gone_items(conn, capture, location_id)]
    return {"existing_items": here + gone}


def apply_extend(
    conn: sqlite3.Connection,
    settings: Settings,
    capture: sqlite3.Row,
    result: ExtractResult,
    image_photo_ids: list[int],
    extras: dict[str, Any],
) -> list[int]:
    """Runs inside the worker's write_tx. Returns the ids of the new items."""
    location_id, moved = resolve_location(conn, capture["location_id"])
    here = {row["id"]: row for row in reconcile.existing_items(conn, location_id)}
    sent = extras["existing_items"]
    sent_ids = {entry["id"] for entry in sent}
    live_sent = {entry["id"] for entry in sent if not entry.get("gone")}
    by_name: dict[str, int] = {}  # safety net: live (i) items only
    for entry in sent:
        if entry["id"] in live_sent:
            by_name.setdefault(_normal(entry["name"]), entry["id"])
    gone_names = {_normal(entry["name"]) for entry in sent if entry["id"] not in live_sent}

    counted: dict[int, int] = {}  # matched (i) item -> quantity the model counted
    found: dict[int, list[boxes.PhotoBox]] = {}
    flagged: set[int] = set()
    new_ids: list[int] = []
    for extracted in normalize_predictions("extend", result):
        matched = extracted.matched_existing_id
        if matched not in sent_ids:
            matched = by_name.get(_normal(extracted.name))
            if matched is not None:
                log.info(
                    "extension %s: %r matches existing item %s by name; flagged for review",
                    capture["id"],
                    extracted.name,
                    matched,
                )
                flagged.add(matched)
        if matched is None:
            like_gone = _normal(extracted.name) in gone_names
            if like_gone:
                log.info(
                    "extension %s: %r is named like an item that is gone; added for review",
                    capture["id"],
                    extracted.name,
                )
            new_ids.append(
                insert_with_boxes(
                    conn,
                    capture,
                    extracted,
                    image_photo_ids,
                    location_id=location_id,
                    needs_review=like_gone or moved or is_uncertain(extracted, settings),
                )
            )
            continue
        found.setdefault(matched, []).extend(boxes.photo_boxes(extracted, image_photo_ids))
        if matched in live_sent and matched in here:
            counted[matched] = counted.get(matched, 0) + extracted.quantity

    capture_id = capture["id"]
    for item_id, item_boxes in found.items():
        boxes.replace_boxes(conn, item_id, capture_id, item_boxes)
    for item_id, quantity in counted.items():
        conn.execute(f"UPDATE items SET last_seen_at = {NOW} WHERE id = ?", (item_id,))
        conn.execute(
            f"UPDATE review_proposals SET status = 'dismissed', resolved_at = {NOW}"
            " WHERE item_id = ? AND type = 'missing' AND status = 'open'",
            (item_id,),
        )
        if quantity > here[item_id]["quantity"]:
            reconcile.propose(conn, "quantity", item_id, capture_id, quantity)
    conn.execute("UPDATE captures SET known_count = ? WHERE id = ?", (len(counted), capture_id))
    for item_id in flagged:
        conn.execute(
            "UPDATE items SET needs_review = 1 WHERE id = ? AND deleted_at IS NULL", (item_id,)
        )
    search.reindex_items(conn, new_ids)
    return new_ids

"""Apply an extraction result (plan 3.6 step 5). The ``apply_*`` functions run inside the
worker's single ``write_tx`` (which also marks the capture done) and reindex what they insert.

``image_photo_ids`` maps the call's image index to its photo id (one entry per image sent,
in order; a PDF's pages share their photo). Photo captures also write each item's boxes
(``item_boxes``) in the same transaction; receipts and text never do.

Raising ``CaptureFailed`` rolls the transaction back and fails the capture with its message.
"""

import re
import sqlite3
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.ai.schemas import ExtractedItem, ExtractResult, Mode
from app.capture import boxes
from app.config import Settings
from app.inventory import items, locations, search

# Second filter for receipt lines that are not products (AC-17); the prompt is the first.
RECEIPT_DENYLIST = re.compile(r"\b(?:tax|total|subtotal|bag|deposit|change|tip)\b", re.IGNORECASE)
MAX_NAME_CHARS = 200


class CaptureFailed(Exception):
    """The capture cannot be applied; the message is stored in ``captures.error``."""


def clean_name(name: str) -> str:
    """Whitespace collapsed and truncated to MAX_NAME_CHARS, as stored on items."""
    return " ".join(name.split())[:MAX_NAME_CHARS]


def normalize_predictions(mode: Mode, result: ExtractResult) -> list[ExtractedItem]:
    """The items a capture of ``mode`` would store from ``result``: receipt lines matching
    the denylist dropped, names cleaned. Shared by capture apply and model comparisons."""
    return [
        item.model_copy(update={"name": clean_name(item.name)})
        for item in result.items
        if not (mode == "receipt" and RECEIPT_DENYLIST.search(item.name))
    ]


def resolve_location(conn: sqlite3.Connection, location_id: int | None) -> tuple[int, bool]:
    """(live location id, needs_review). A soft-deleted location resolves to its nearest live
    ancestor, or the Inbox, with needs_review; no location at all means the Inbox too."""
    if location_id is not None:
        if locations.get(conn, location_id) is not None:
            return location_id, False
        for row in reversed(locations.ancestors(conn, location_id)[:-1]):
            if row["deleted_at"] is None:
                return row["id"], True
    return locations.inbox(conn)["id"], True


def insert_extracted(
    conn: sqlite3.Connection,
    capture: sqlite3.Row,
    item: ExtractedItem,
    *,
    location_id: int,
    needs_review: bool,
    photo_id: int | None,
    price_cents: int | None = None,
    purchase_date: str | None = None,
) -> int:
    return items.insert(
        conn,
        items.ItemData(
            name=clean_name(item.name),
            location_id=location_id,
            quantity=item.quantity,
            kind=item.kind,
            category=item.category,
            purchase_date=purchase_date,
            price_cents=price_cents,
        ),
        items.SystemFields(
            photo_id=photo_id,
            capture_id=capture["id"],
            confidence=item.confidence,
            needs_review=needs_review,
        ),
    )


def is_uncertain(item: ExtractedItem, settings: Settings) -> bool:
    return item.confidence < settings.ai_confidence_threshold


def insert_with_boxes(
    conn: sqlite3.Connection,
    capture: sqlite3.Row,
    item: ExtractedItem,
    image_photo_ids: list[int],
    *,
    location_id: int,
    needs_review: bool,
) -> int:
    """Insert a photo-capture item and its boxes. Its ``photo_id`` is the first photo (by
    position) with one of its boxes, else the call's first photo (R4)."""
    found = boxes.photo_boxes(item, image_photo_ids)
    item_id = insert_extracted(
        conn,
        capture,
        item,
        location_id=location_id,
        needs_review=needs_review,
        photo_id=boxes.first_boxed_photo(found, image_photo_ids),
    )
    boxes.replace_boxes(conn, item_id, capture["id"], found)
    return item_id


def apply_shelf(
    conn: sqlite3.Connection,
    settings: Settings,
    capture: sqlite3.Row,
    result: ExtractResult,
    image_photo_ids: list[int],
    extras: dict[str, Any],
) -> list[int]:
    """Photo capture: every item goes to the capture's location (AC-13, AC-14), with its
    boxes on the capture's photos."""
    location_id, moved = resolve_location(conn, capture["location_id"])
    ids = [
        insert_with_boxes(
            conn,
            capture,
            item,
            image_photo_ids,
            location_id=location_id,
            needs_review=moved or is_uncertain(item, settings),
        )
        for item in normalize_predictions("shelf", result)
    ]
    search.reindex_items(conn, ids)
    return ids


def _capture_date(capture: sqlite3.Row, settings: Settings) -> date:
    created = datetime.fromisoformat(capture["created_at"].replace("Z", "+00:00"))
    return created.astimezone(ZoneInfo(settings.tz)).date()


def apply_receipt(
    conn: sqlite3.Connection,
    settings: Settings,
    capture: sqlite3.Row,
    result: ExtractResult,
    image_photo_ids: list[int],
    extras: dict[str, Any],
) -> list[int]:
    """Receipt: product lines only, into the Inbox for review, with price and purchase date
    (line date, then the receipt's date, then the capture's local date) (AC-17). No boxes."""
    photo_id = image_photo_ids[0] if image_photo_ids else None
    inbox_id = locations.inbox(conn)["id"]
    meta = result.receipt
    fallback = (meta.purchase_date if meta else None) or _capture_date(capture, settings)
    ids = [
        insert_extracted(
            conn,
            capture,
            item,
            location_id=inbox_id,
            needs_review=True,
            photo_id=photo_id,
            price_cents=item.price_cents,
            purchase_date=(item.purchase_date or fallback).isoformat(),
        )
        for item in normalize_predictions("receipt", result)
    ]
    merchant = meta.merchant.strip() if meta and meta.merchant else None
    conn.execute("UPDATE captures SET merchant = ? WHERE id = ?", (merchant, capture["id"]))
    search.reindex_items(conn, ids)
    return ids


def known_location_names(conn: sqlite3.Connection) -> list[str]:
    """Live location names offered to the model in text mode (deduplicated, sorted)."""
    rows = conn.execute("SELECT DISTINCT name FROM locations WHERE deleted_at IS NULL").fetchall()
    return sorted((row[0] for row in rows), key=str.casefold)


def match_location_hint(conn: sqlite3.Connection, hint: str | None) -> int | None:
    """A live location for ``hint``: the unique case-insensitive exact name, else the unique
    name with that prefix. Ambiguous or unknown hints give None."""
    needle = " ".join((hint or "").split()).casefold()
    if not needle:
        return None
    rows = conn.execute("SELECT id, name FROM locations WHERE deleted_at IS NULL").fetchall()
    exact = [row["id"] for row in rows if row["name"].casefold() == needle]
    if exact:
        return exact[0] if len(exact) == 1 else None
    prefixed = [row["id"] for row in rows if row["name"].casefold().startswith(needle)]
    return prefixed[0] if len(prefixed) == 1 else None


def apply_text(
    conn: sqlite3.Connection,
    settings: Settings,
    capture: sqlite3.Row,
    result: ExtractResult,
    image_photo_ids: list[int],
    extras: dict[str, Any],
) -> list[int]:
    """Text/voice: a matched ``location_hint`` wins; otherwise the capture's location (or the
    Inbox). An unmatched hint, or no location at all, marks the item for review (AC-18)."""
    photo_id = image_photo_ids[0] if image_photo_ids else None
    fallback_id, fallback_review = resolve_location(conn, capture["location_id"])
    ids = []
    for item in normalize_predictions("text", result):
        matched = match_location_hint(conn, item.location_hint)
        if matched is not None:
            location_id, review = matched, False
        else:
            location_id = fallback_id
            review = fallback_review or bool(item.location_hint and item.location_hint.strip())
        ids.append(
            insert_extracted(
                conn,
                capture,
                item,
                location_id=location_id,
                needs_review=review or is_uncertain(item, settings),
                photo_id=photo_id,
            )
        )
    search.reindex_items(conn, ids)
    return ids

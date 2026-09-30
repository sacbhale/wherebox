"""Apply an extraction result (plan 3.6 step 5). The ``apply_*`` functions run inside the
worker's single ``write_tx`` (which also marks the capture done) and reindex what they insert.

Raising ``CaptureFailed`` rolls the transaction back and fails the capture with its message.
"""

import re
import sqlite3
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.ai.schemas import ExtractedItem, ExtractResult
from app.config import Settings
from app.inventory import items, locations, search

# Second filter for receipt lines that are not products (AC-17); the prompt is the first.
RECEIPT_DENYLIST = re.compile(r"\b(?:tax|total|subtotal|bag|deposit|change|tip)\b", re.IGNORECASE)


class CaptureFailed(Exception):
    """The capture cannot be applied; the message is stored in ``captures.error``."""


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
            name=" ".join(item.name.split())[:200],
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


def apply_shelf(
    conn: sqlite3.Connection,
    settings: Settings,
    capture: sqlite3.Row,
    result: ExtractResult,
    photo_id: int | None,
    extras: dict[str, Any],
) -> list[int]:
    """Photo capture: every item goes to the capture's location (AC-13, AC-14)."""
    location_id, moved = resolve_location(conn, capture["location_id"])
    ids = [
        insert_extracted(
            conn,
            capture,
            item,
            location_id=location_id,
            needs_review=moved or is_uncertain(item, settings),
            photo_id=photo_id,
        )
        for item in result.items
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
    photo_id: int | None,
    extras: dict[str, Any],
) -> list[int]:
    """Receipt: product lines only, into the Inbox for review, with price and purchase date
    (line date, then the receipt's date, then the capture's local date) (AC-17)."""
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
        for item in result.items
        if not RECEIPT_DENYLIST.search(item.name)
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
    photo_id: int | None,
    extras: dict[str, Any],
) -> list[int]:
    """Text/voice: a matched ``location_hint`` wins; otherwise the capture's location (or the
    Inbox). An unmatched hint, or no location at all, marks the item for review (AC-18)."""
    fallback_id, fallback_review = resolve_location(conn, capture["location_id"])
    ids = []
    for item in result.items:
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

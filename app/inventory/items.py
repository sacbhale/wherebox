"""Item repository. Every write runs in ``write_tx`` and reindexes FTS in the same transaction.

``insert`` is the low-level building block for batch writers (capture apply): call it inside
your own ``write_tx`` and reindex the returned ids once at the end.
"""

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.db.connection import write_tx
from app.db.timestamps import NOW
from app.inventory import categories, locations, search

KINDS = search.KINDS
MAX_QUANTITY = 1_000_000


class ItemError(ValueError):
    """A rejected item change (HTTP 400)."""


@dataclass(frozen=True)
class ItemData:
    """User-editable fields (AC-1). ``category`` is a name, resolved with get_or_create."""

    name: str
    location_id: int
    quantity: int = 1
    kind: str = "belonging"
    category: str | None = None
    expiry_date: str | None = None  # YYYY-MM-DD
    purchase_date: str | None = None  # YYYY-MM-DD
    price_cents: int | None = None
    notes: str | None = None


@dataclass(frozen=True)
class SystemFields:
    """Fields managed by the capture pipeline, never by the manual form."""

    photo_id: int | None = None
    capture_id: int | None = None
    confidence: float | None = None
    needs_review: bool = False


def parse_price(text: str) -> int | None:
    """``"3.49"`` -> 349 cents; blank -> None. Raises ItemError."""
    text = text.strip().replace(",", ".").lstrip("$")
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ItemError("Price must be a number like 3.49.") from exc
    if not value.is_finite() or value < 0 or value >= 10_000_000:
        raise ItemError("Price must be between 0 and 9,999,999.99.")
    if value != value.quantize(Decimal("0.01")):
        raise ItemError("Price can have at most 2 decimal places.")
    return int(value * 100)


def format_price(cents: int | None) -> str:
    return "" if cents is None else f"{cents // 100}.{cents % 100:02d}"


def _parse_date(text: str, label: str) -> str | None:
    text = text.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ItemError(f"{label} must be a date (YYYY-MM-DD).") from exc


def clean_user_name(name: str) -> str:
    """A name typed by the user: whitespace collapsed, 1-200 characters. Raises ItemError."""
    name = " ".join(name.split())
    if not name:
        raise ItemError("Name is required.")
    if len(name) > 200:
        raise ItemError("Name is too long.")
    return name


def parse_form(form: Mapping[str, str]) -> tuple[ItemData | None, dict[str, str]]:
    """Validate manual-form strings. Returns (data, {}) or (None, {field: message})."""
    errors: dict[str, str] = {}

    def field(key: str) -> str:
        return (form.get(key) or "").strip()

    try:
        name = clean_user_name(field("name"))
    except ItemError as exc:
        errors["name"] = str(exc)
        name = ""
    try:
        quantity = int(field("quantity") or "1")
        if not 0 <= quantity <= MAX_QUANTITY:
            raise ValueError
    except ValueError:
        errors["quantity"] = "Quantity must be a whole number, 0 or more."
        quantity = 0
    kind = field("kind") or "belonging"
    if kind not in KINDS:
        errors["kind"] = "Choose consumable or belonging."
    try:
        location_id = int(field("location_id"))
    except ValueError:
        errors["location_id"] = "Choose a location."
        location_id = 0
    values: dict[str, str | int | None] = {}
    for key, parse in (
        ("expiry_date", lambda v: _parse_date(v, "Expiry date")),
        ("purchase_date", lambda v: _parse_date(v, "Purchase date")),
        ("price", parse_price),
    ):
        try:
            values[key] = parse(field(key))
        except ItemError as exc:
            errors[key] = str(exc)
    if errors:
        return None, errors
    return (
        ItemData(
            name=name,
            location_id=location_id,
            quantity=quantity,
            kind=kind,
            category=field("category") or None,
            expiry_date=values["expiry_date"],  # type: ignore[arg-type]
            purchase_date=values["purchase_date"],  # type: ignore[arg-type]
            price_cents=values["price"],  # type: ignore[arg-type]
            notes=(form.get("notes") or "").strip() or None,
        ),
        {},
    )


def get(
    conn: sqlite3.Connection, item_id: int, include_deleted: bool = False
) -> sqlite3.Row | None:
    """An item with ``category_name``, ``location_code`` and ``location_path`` (None once
    used up: its FTS row is gone)."""
    return conn.execute(
        "SELECT i.*, c.name AS category_name, l.short_code AS location_code, "
        "f.location_path AS location_path FROM items i "
        "JOIN locations l ON l.id = i.location_id "
        "LEFT JOIN items_fts f ON f.rowid = i.id "
        "LEFT JOIN categories c ON c.id = i.category_id "
        "WHERE i.id = ?" + ("" if include_deleted else " AND i.deleted_at IS NULL"),
        (item_id,),
    ).fetchone()


def list_in(conn: sqlite3.Connection, location_ids: Iterable[int]) -> list[sqlite3.Row]:
    """Live items in the given locations, with ``location_path`` and ``category_name``."""
    ids = list(location_ids)
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    return conn.execute(
        "SELECT i.*, f.location_path AS location_path, l.short_code AS location_code, "
        "c.name AS category_name FROM items i "
        "JOIN items_fts f ON f.rowid = i.id "
        "JOIN locations l ON l.id = i.location_id "
        "LEFT JOIN categories c ON c.id = i.category_id "
        f"WHERE i.deleted_at IS NULL AND i.location_id IN ({marks}) "
        "ORDER BY i.name COLLATE NOCASE, i.id",
        ids,
    ).fetchall()


def _require_location(conn: sqlite3.Connection, location_id: int) -> None:
    if locations.get(conn, location_id) is None:
        raise ItemError("That location does not exist.")


def insert(conn: sqlite3.Connection, data: ItemData, system: SystemFields | None = None) -> int:
    """Insert one item. Call inside write_tx(); the caller reindexes the returned id."""
    system = system or SystemFields()
    if data.kind not in KINDS:
        raise ItemError("Choose consumable or belonging.")
    if data.quantity < 0:
        raise ItemError("Quantity cannot be negative.")
    _require_location(conn, data.location_id)
    item_id = conn.execute(
        "INSERT INTO items (name, quantity, category_id, kind, location_id, expiry_date, "
        "purchase_date, price_cents, notes, photo_id, capture_id, confidence, needs_review) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data.name,
            data.quantity,
            categories.get_or_create(conn, data.category),
            data.kind,
            data.location_id,
            data.expiry_date,
            data.purchase_date,
            data.price_cents,
            data.notes,
            system.photo_id,
            system.capture_id,
            system.confidence,
            int(system.needs_review),
        ),
    ).lastrowid
    locations.mark_used(conn, data.location_id)
    return item_id


def create(conn: sqlite3.Connection, data: ItemData, system: SystemFields | None = None) -> int:
    with write_tx(conn):
        item_id = insert(conn, data, system)
        search.reindex_items(conn, [item_id])
    return item_id


def update(
    conn: sqlite3.Connection, item_id: int, data: ItemData, clear_review: bool = False
) -> None:
    """Save the manual form. System fields (photo, capture, confidence...) are untouched,
    except that ``clear_review`` (an edit made from the review queue) clears needs_review."""
    with write_tx(conn):
        old = get(conn, item_id)
        if old is None:
            raise ItemError("Item not found.")
        if data.kind not in KINDS:
            raise ItemError("Choose consumable or belonging.")
        _require_location(conn, data.location_id)
        conn.execute(
            "UPDATE items SET name = ?, quantity = ?, category_id = ?, kind = ?, "
            "location_id = ?, expiry_date = ?, purchase_date = ?, price_cents = ?, notes = ?, "
            f"needs_review = CASE WHEN ? THEN 0 ELSE needs_review END, updated_at = {NOW} "
            "WHERE id = ?",
            (
                data.name,
                data.quantity,
                categories.get_or_create(conn, data.category),
                data.kind,
                data.location_id,
                data.expiry_date,
                data.purchase_date,
                data.price_cents,
                data.notes,
                int(clear_review),
                item_id,
            ),
        )
        if old["location_id"] != data.location_id:
            locations.mark_used(conn, data.location_id)
        search.reindex_items(conn, [item_id])


def adjust_quantity(conn: sqlite3.Connection, item_id: int, delta: int) -> int | None:
    """Add ``delta`` to the quantity, flooring at 0 (the item is kept at 0).
    Returns the new quantity, or None if the item is missing or used up.
    Quantity is not indexed, so no reindex is needed."""
    with write_tx(conn):
        row = conn.execute(
            "UPDATE items SET quantity = min(max(quantity + ?, 0), ?), "
            f"updated_at = {NOW} WHERE id = ? AND deleted_at IS NULL RETURNING quantity",
            (delta, MAX_QUANTITY, item_id),
        ).fetchone()
    return None if row is None else row[0]


def rename(conn: sqlite3.Connection, item_id: int, name: str) -> None:
    """Inline rename: one UPDATE and the FTS reindex (needs_review unchanged). Raises
    ItemError for a bad name or an item that is gone."""
    name = clean_user_name(name)
    with write_tx(conn):
        cur = conn.execute(
            f"UPDATE items SET name = ?, updated_at = {NOW} WHERE id = ? AND deleted_at IS NULL",
            (name, item_id),
        )
        if cur.rowcount == 0:
            raise ItemError("Item not found.")
        search.reindex_items(conn, [item_id])


def _soft_delete(conn: sqlite3.Connection, item_id: int, reason: str) -> bool:
    with write_tx(conn):
        cur = conn.execute(
            f"UPDATE items SET deleted_at = {NOW}, deleted_reason = ?, updated_at = {NOW} "
            "WHERE id = ? AND deleted_at IS NULL",
            (reason, item_id),
        )
        if cur.rowcount == 0:
            return False
        search.remove_items(conn, [item_id])
    return True


def use_up(conn: sqlite3.Connection, item_id: int) -> bool:
    """ "Used up": soft-delete the item and drop its FTS row. False if already gone."""
    return _soft_delete(conn, item_id, "used_up")


def delete_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """ "Delete" (a wrong detection): the same soft delete as ``use_up`` (boxes and photos
    kept, undone by ``undo_use_up``) with ``deleted_reason = 'deleted'``, which keeps it out
    of comparison prefills and capture galleries. False if already gone."""
    return _soft_delete(conn, item_id, "deleted")


def undo_use_up(conn: sqlite3.Connection, item_id: int) -> bool:
    """Restore a used-up or deleted item (and its FTS row). False if it was not gone."""
    with write_tx(conn):
        row = conn.execute(
            "SELECT location_id FROM items WHERE id = ? AND deleted_at IS NOT NULL", (item_id,)
        ).fetchone()
        if row is None:
            return False
        location_id = row[0]
        if locations.get(conn, location_id) is None:  # defensive: deletes re-parent items
            location_id = locations.inbox(conn)["id"]
        conn.execute(
            "UPDATE items SET deleted_at = NULL, deleted_reason = NULL, location_id = ?,"
            f" updated_at = {NOW} WHERE id = ?",
            (location_id, item_id),
        )
        search.reindex_items(conn, [item_id])
    return True


def move_items(
    conn: sqlite3.Connection, item_ids: Iterable[int], location_id: int
) -> dict[int, int]:
    """Move live items to a live location. Returns {item_id: previous location_id} for the
    items actually moved (use it to undo by moving each back)."""
    ids = list(dict.fromkeys(item_ids))
    with write_tx(conn):
        _require_location(conn, location_id)
        previous: dict[int, int] = {}
        for item_id in ids:
            row = conn.execute(
                "SELECT location_id FROM items WHERE id = ? AND deleted_at IS NULL", (item_id,)
            ).fetchone()
            if row is not None and row[0] != location_id:
                previous[item_id] = row[0]
        conn.executemany(
            f"UPDATE items SET location_id = ?, updated_at = {NOW} WHERE id = ?",
            [(location_id, item_id) for item_id in previous],
        )
        if previous:
            locations.mark_used(conn, location_id)
            search.reindex_items(conn, previous)
    return previous


def move_item(conn: sqlite3.Connection, item_id: int, location_id: int) -> int | None:
    """Move one item. Returns its previous location id, or None if nothing moved."""
    return move_items(conn, [item_id], location_id).get(item_id)

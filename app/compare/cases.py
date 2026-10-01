"""Comparison cases repository: a finished photo, receipt or text capture plus the items the
user says it really contains (``benchmark_cases``).

A case is created from its capture's items (pre-filled, then corrected by the user), can be
edited, and is soft-deleted (``deleted_at``): hidden from new runs, still shown in old
reports. Saving a case for a capture whose case was deleted brings that case back. The
original input (photos/PDF under DATA_DIR, or the capture's text) must still exist, otherwise
the case is flagged "input missing" and skipped by new runs.

Multi-photo (spec-multiphoto R9): a case is keyed on its capture group's **root** capture;
``photo_ids_json`` lists the group's photos (root and done extensions, in order) when the case
is created, and a run sends all of them. Older cases (NULL) use their capture's photos.
"""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from app.capture import groups
from app.compare.scoring import Expected
from app.config import Settings
from app.db.connection import write_tx
from app.db.timestamps import NOW
from app.inventory.items import ItemError, parse_price

ELIGIBLE_TYPES = ("photo", "receipt", "text")
MAX_ROWS = 200
MAX_NAME_CHARS = 200
MAX_QUANTITY = 1_000_000
INPUT_MISSING = "input missing"

_SELECT = (
    "SELECT b.*, c.type, c.status AS capture_status, c.provider AS capture_provider,"
    " c.model AS capture_model, c.input_text, c.created_at AS capture_created_at,"
    " l.name AS location_name"
    " FROM benchmark_cases b JOIN captures c ON c.id = b.capture_id"
    " LEFT JOIN locations l ON l.id = c.location_id"
)


class CaseError(ValueError):
    """The case cannot be saved; the message is shown to the user."""


@dataclass(frozen=True)
class Case:
    id: int
    capture_id: int
    type: str
    expected: list[Expected]
    deleted_at: str | None
    created_at: str
    updated_at: str
    capture_provider: str | None
    capture_model: str | None
    capture_created_at: str
    location_name: str | None
    input_text: str | None
    photo_ids: list[int]  # the photos a run sends, in order ([] for text)
    rel_paths: list[str]  # their originals (relative to DATA_DIR), same order
    input_missing: bool

    @property
    def photo_id(self) -> int | None:
        """The first photo (the "Open the original" link)."""
        return self.photo_ids[0] if self.photo_ids else None

    @property
    def rel_path(self) -> str | None:
        return self.rel_paths[0] if self.rel_paths else None

    @property
    def image_count(self) -> int:
        """Photos a run sends (a PDF receipt may render up to 3 pages: counted as 1)."""
        return len(self.photo_ids)

    @property
    def usable(self) -> bool:
        """Live and with its input on hand: a new run can use it."""
        return self.deleted_at is None and not self.input_missing

    @property
    def label(self) -> str:
        where = f" · {self.location_name}" if self.location_name else ""
        return f"{self.type.capitalize()} {self.capture_created_at[:10]}{where}"


def dump_expected(items: Sequence[Expected]) -> str:
    return json.dumps(
        [{"name": e.name, "quantity": e.quantity, "price_cents": e.price_cents} for e in items]
    )


def load_expected(text: str) -> list[Expected]:
    return [
        Expected(str(e["name"]), int(e.get("quantity", 1)), e.get("price_cents"))
        for e in json.loads(text)
    ]


def _file_missing(settings: Settings, rel: str | None) -> bool:
    if not rel:
        return True
    root = settings.photos_dir.resolve()
    path = (settings.data_dir / rel).resolve()
    return not path.is_relative_to(root) or not path.is_file()


def input_missing(
    settings: Settings, capture_type: str, text: str | None, rels: Sequence[str | None]
) -> bool:
    """True when the capture's original input is gone: no text, no photo, or any listed
    photo file deleted."""
    if capture_type == "text":
        return not (text or "").strip()
    return not rels or any(_file_missing(settings, rel) for rel in rels)


def _photos(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[list[int], list[str | None]]:
    """The case's photo ids (``photo_ids_json``, else its capture's photos) and their
    rel_paths (None for a photo row that no longer exists)."""
    if row["photo_ids_json"] is not None:
        ids = [int(photo_id) for photo_id in json.loads(row["photo_ids_json"])]
    else:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM photos WHERE capture_id = ? ORDER BY position, id",
                (row["capture_id"],),
            )
        ]
    if not ids:
        return [], []
    marks = ",".join("?" * len(ids))
    paths = dict(
        conn.execute(f"SELECT id, rel_path FROM photos WHERE id IN ({marks})", ids).fetchall()
    )
    return ids, [paths.get(photo_id) for photo_id in ids]


def _case(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> Case:
    photo_ids, rel_paths = _photos(conn, row)
    return Case(
        id=row["id"],
        capture_id=row["capture_id"],
        type=row["type"],
        expected=load_expected(row["expected_json"]),
        deleted_at=row["deleted_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        capture_provider=row["capture_provider"],
        capture_model=row["capture_model"],
        capture_created_at=row["capture_created_at"],
        location_name=row["location_name"],
        input_text=row["input_text"],
        photo_ids=photo_ids,
        rel_paths=[rel or "" for rel in rel_paths],
        input_missing=input_missing(settings, row["type"], row["input_text"], rel_paths),
    )


def get(conn: sqlite3.Connection, settings: Settings, case_id: int) -> Case | None:
    """A case by id, soft-deleted ones included."""
    row = conn.execute(_SELECT + " WHERE b.id = ?", (case_id,)).fetchone()
    return _case(conn, settings, row) if row else None


def for_capture(conn: sqlite3.Connection, settings: Settings, capture_id: int) -> Case | None:
    row = conn.execute(_SELECT + " WHERE b.capture_id = ?", (capture_id,)).fetchone()
    return _case(conn, settings, row) if row else None


def many(conn: sqlite3.Connection, settings: Settings, case_ids: Sequence[int]) -> dict[int, Case]:
    """Cases by id (soft-deleted included), for reports."""
    if not case_ids:
        return {}
    marks = ",".join("?" * len(case_ids))
    rows = conn.execute(_SELECT + f" WHERE b.id IN ({marks})", tuple(case_ids)).fetchall()
    return {row["id"]: _case(conn, settings, row) for row in rows}


def live(conn: sqlite3.Connection, settings: Settings) -> list[Case]:
    """Cases not deleted, newest capture first."""
    rows = conn.execute(
        _SELECT + " WHERE b.deleted_at IS NULL ORDER BY c.created_at DESC, b.id DESC"
    ).fetchall()
    return [_case(conn, settings, row) for row in rows]


def count_live(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM benchmark_cases WHERE deleted_at IS NULL").fetchone()[
        0
    ]


# ---------------------------------------------------------------- making a case


def ineligible_reason(capture: sqlite3.Row | None) -> str | None:
    """Why this capture cannot become a case, or None."""
    if capture is None:
        return "This capture does not exist."
    if capture["type"] not in ELIGIBLE_TYPES:
        return "Only photo, receipt and text captures can be comparison cases."
    if capture["status"] != "done":
        return "Only finished captures can be comparison cases."
    return None


@dataclass(frozen=True)
class Prefill:
    items: list[Expected]
    changed: bool  # some of the capture's items were edited, moved or used up since


def case_capture_id(conn: sqlite3.Connection, capture_id: int) -> int:
    """The capture a case of ``capture_id`` is keyed on: its group's root (an extension's
    "Use for model comparison" goes to the root). Unknown ids are returned unchanged."""
    row = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    return groups.root_id(row) if row is not None else capture_id


def prefill(conn: sqlite3.Connection, capture_id: int) -> Prefill:
    """The items the capture's group created (the root and its done extensions; current name
    and quantity, and price), used up ones included, in creation order."""
    ids = groups.done_capture_ids(conn, case_capture_id(conn, capture_id))
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT name, quantity, price_cents, deleted_at, created_at, updated_at FROM items"
        f" WHERE capture_id IN ({marks}) ORDER BY id",
        ids,
    ).fetchall()
    changed = any(r["deleted_at"] is not None or r["updated_at"] != r["created_at"] for r in rows)
    return Prefill(
        [Expected(r["name"], max(r["quantity"], 1), r["price_cents"]) for r in rows], changed
    )


def parse_rows(
    names: Sequence[str],
    quantities: Sequence[str],
    prices: Sequence[str],
    *,
    with_prices: bool,
) -> tuple[list[Expected], list[str]]:
    """The form's rows (blank names skipped). Returns (items, error messages)."""
    items: list[Expected] = []
    errors: list[str] = []
    for index, raw_name in enumerate(names):
        name = " ".join(raw_name.split())
        if not name:
            continue
        row = index + 1
        if len(name) > MAX_NAME_CHARS:
            errors.append(f"Row {row}: keep the name under {MAX_NAME_CHARS} characters.")
            continue
        raw_qty = (quantities[index] if index < len(quantities) else "").strip() or "1"
        try:
            quantity = int(raw_qty)
        except ValueError:
            quantity = 0
        if not 1 <= quantity <= MAX_QUANTITY:
            errors.append(f"Row {row}: quantity must be a whole number of at least 1.")
            continue
        price = None
        if with_prices:
            try:
                price = parse_price(prices[index] if index < len(prices) else "")
            except ItemError as exc:
                errors.append(f"Row {row}: {exc}")
                continue
        items.append(Expected(name, quantity, price))
    if len(items) > MAX_ROWS:
        errors.append(f"A case can have at most {MAX_ROWS} items.")
    if not items and not errors:
        errors.append("Add at least one item.")
    return items, errors


def save(conn: sqlite3.Connection, capture_id: int, items: Sequence[Expected]) -> int:
    """Create the case for ``capture_id``'s group root (or update and restore its existing
    one), listing the group's photos (root and done extensions) as they are now. Raises
    CaseError when the capture is not eligible. Returns the case id."""
    with write_tx(conn):
        capture_id = case_capture_id(conn, capture_id)
        capture = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
        reason = ineligible_reason(capture)
        if reason:
            raise CaseError(reason)
        photo_ids = json.dumps(groups.done_photo_ids(conn, capture_id))
        row = conn.execute(
            "INSERT INTO benchmark_cases (capture_id, expected_json, photo_ids_json)"
            " VALUES (?, ?, ?)"
            " ON CONFLICT(capture_id) DO UPDATE SET expected_json = excluded.expected_json,"
            " photo_ids_json = excluded.photo_ids_json,"
            f" deleted_at = NULL, updated_at = {NOW} RETURNING id",
            (capture_id, dump_expected(items), photo_ids),
        ).fetchone()
    return row[0]


def update(conn: sqlite3.Connection, case_id: int, items: Sequence[Expected]) -> bool:
    """Replace a live case's expected items. False when there is no such live case."""
    with write_tx(conn):
        cursor = conn.execute(
            f"UPDATE benchmark_cases SET expected_json = ?, updated_at = {NOW}"
            " WHERE id = ? AND deleted_at IS NULL",
            (dump_expected(items), case_id),
        )
    return cursor.rowcount == 1


def soft_delete(conn: sqlite3.Connection, case_id: int) -> bool:
    """Hide a case from new runs (old reports keep it). False when already deleted/unknown."""
    with write_tx(conn):
        cursor = conn.execute(
            f"UPDATE benchmark_cases SET deleted_at = {NOW}, updated_at = {NOW}"
            " WHERE id = ? AND deleted_at IS NULL",
            (case_id,),
        )
    return cursor.rowcount == 1


def capture_photo_id(conn: sqlite3.Connection, capture_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM photos WHERE capture_id = ? ORDER BY id LIMIT 1", (capture_id,)
    ).fetchone()
    return row[0] if row else None


def photo_paths(settings: Settings, case: Case) -> list[tuple[int, Path]]:
    """(photo id, original photo/PDF) of every photo the case sends, in order ([] for text)."""
    return [
        (photo_id, settings.data_dir / rel)
        for photo_id, rel in zip(case.photo_ids, case.rel_paths, strict=True)
    ]

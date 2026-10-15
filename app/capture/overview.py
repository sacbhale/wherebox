"""A location's photo ("contents picture"): the read side. The writes (pin / unpin) live in
``app.inventory.locations``."""

import sqlite3
from dataclasses import dataclass

from app.capture import groups

# A photo the app can show: it has a display JPEG and a display size (alias ``p``).
DISPLAYABLE = "p.display_rel_path IS NOT NULL AND p.width IS NOT NULL AND p.height IS NOT NULL"


@dataclass(frozen=True)
class Overview:
    """A location's photo ("contents picture"), see ``overview``. ``root_capture_id``: the
    capture group it belongs to ("All photos")."""

    photo_id: int
    width: int | None
    height: int | None
    root_capture_id: int
    pinned: bool


_OVERVIEW_PHOTO = (
    "SELECT p.id, p.width, p.height, coalesce(c.parent_capture_id, c.id) AS root_id"
    f" FROM photos p JOIN captures c ON c.id = p.capture_id WHERE p.id = ? AND {DISPLAYABLE}"
)


def _auto_photo(conn: sqlite3.Connection, capture: sqlite3.Row) -> int | None:
    """The capture's displayable call photo: the one the model named, else the one with the
    most boxes of live items, else the first."""
    photo_ids = [row["id"] for row in groups.call_photos(conn, capture)]
    if not photo_ids:
        return None
    marks = ",".join("?" * len(photo_ids))
    shown = {
        row[0]
        for row in conn.execute(
            f"SELECT p.id FROM photos p WHERE p.id IN ({marks}) AND {DISPLAYABLE}", photo_ids
        )
    }
    photo_ids = [photo_id for photo_id in photo_ids if photo_id in shown]
    if not photo_ids:
        return None
    if capture["overview_photo_id"] in shown:
        return capture["overview_photo_id"]
    marks = ",".join("?" * len(photo_ids))
    counts = dict(
        conn.execute(
            "SELECT b.photo_id, count(*) FROM item_boxes b JOIN items i ON i.id = b.item_id"
            f" WHERE i.deleted_at IS NULL AND b.photo_id IN ({marks}) GROUP BY b.photo_id",
            photo_ids,
        ).fetchall()
    )
    return max(photo_ids, key=lambda photo_id: counts.get(photo_id, 0))


def overview(conn: sqlite3.Connection, location: sqlite3.Row) -> Overview | None:
    """The location's photo: the pinned one (``locations.overview_photo_id``) while it still
    exists and is displayable; else, from the latest done photo/reconcile capture of the
    location, its ``overview_photo_id`` (set by apply), else its displayable call photo with
    the most boxes of live items, else its first displayable photo. None for the Inbox and
    without such a capture."""
    if location["is_system"]:
        return None
    pinned = location["overview_photo_id"]
    if pinned is not None:
        row = conn.execute(_OVERVIEW_PHOTO, (pinned,)).fetchone()
        if row is not None:
            return Overview(row["id"], row["width"], row["height"], row["root_id"], True)
    capture = conn.execute(
        "SELECT * FROM captures WHERE status = 'done' AND type IN ('photo', 'reconcile')"
        " AND location_id = ? ORDER BY id DESC LIMIT 1",
        (location["id"],),
    ).fetchone()
    if capture is None:
        return None
    photo_id = _auto_photo(conn, capture)
    row = conn.execute(_OVERVIEW_PHOTO, (photo_id,)).fetchone() if photo_id else None
    if row is None:
        return None
    return Overview(row["id"], row["width"], row["height"], row["root_id"], False)

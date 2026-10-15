"""Capture groups (spec-multiphoto Parts 2 and 4, R7): a root photo capture plus its "add
another angle" extensions (``captures.parent_capture_id`` = the root's id; an extension of an
extension attaches to the root). Read-only: the photos a provider call sends, the group
limits, and the group view used by the capture detail page / result gallery.

Photo order everywhere: the root's photos, then each extension's (oldest first), each
capture's photos by ``position, id``.
"""

import sqlite3
from dataclasses import dataclass, field

MAX_PHOTOS_PER_SUBMISSION = 6
MAX_GROUP_PHOTOS = 12
# Captures whose photos count toward the group limit (failed ones do not).
COUNTED_STATUSES = ("queued", "processing", "done")


def root_id(capture: sqlite3.Row) -> int:
    """The id of the capture's group root (itself for a root capture)."""
    return capture["parent_capture_id"] or capture["id"]


def call_photos(conn: sqlite3.Connection, capture: sqlite3.Row) -> list[sqlite3.Row]:
    """The photos this capture's provider call sends, in order. A root capture sends its own
    photos; an extension sends the root's and every *done* extension's photos, then its own
    (failed or queued siblings are left out). Re-read at prepare time, so the retry of a
    failed extension sees the current group."""
    if capture["parent_capture_id"] is None:
        return conn.execute(
            "SELECT * FROM photos WHERE capture_id = ? ORDER BY position, id", (capture["id"],)
        ).fetchall()
    root = capture["parent_capture_id"]
    return conn.execute(
        "SELECT p.* FROM photos p JOIN captures c ON c.id = p.capture_id"
        " WHERE c.id = ? OR c.id = ? OR (c.parent_capture_id = ? AND c.status = 'done')"
        " ORDER BY CASE WHEN c.id = ? THEN 0 WHEN c.id = ? THEN 2 ELSE 1 END, c.id,"
        " p.position, p.id",
        (root, capture["id"], root, root, capture["id"]),
    ).fetchall()


def done_capture_ids(conn: sqlite3.Connection, root: int) -> list[int]:
    """The root and its done extensions, oldest first."""
    rows = conn.execute(
        "SELECT id FROM captures WHERE id = ? OR (parent_capture_id = ? AND status = 'done')"
        " ORDER BY id",
        (root, root),
    ).fetchall()
    return [row[0] for row in rows]


def done_photo_ids(conn: sqlite3.Connection, root: int) -> list[int]:
    """Photo ids of the root and its done extensions, in group order."""
    ids = done_capture_ids(conn, root)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT p.id FROM photos p WHERE p.capture_id IN ({marks})"
        " ORDER BY CASE WHEN p.capture_id = ? THEN 0 ELSE 1 END, p.capture_id, p.position, p.id",
        (*ids, root),
    ).fetchall()
    return [row[0] for row in rows]


def counted_photos(conn: sqlite3.Connection, root: int) -> list[sqlite3.Row]:
    """(id, capture_id, sha256) of the group's photos that count toward MAX_GROUP_PHOTOS:
    those of its queued, processing and done captures."""
    marks = ",".join("?" * len(COUNTED_STATUSES))
    return conn.execute(
        "SELECT p.id, p.capture_id, p.sha256 FROM photos p JOIN captures c ON c.id = p.capture_id"
        f" WHERE (c.id = ? OR c.parent_capture_id = ?) AND c.status IN ({marks})",
        (root, root, *COUNTED_STATUSES),
    ).fetchall()


def extend_refusal(capture: sqlite3.Row | None) -> str | None:
    """Why ``capture`` cannot get another angle, or None: only a done photo capture (root
    or extension) can."""
    if capture is None:
        return "This capture does not exist."
    if capture["type"] != "photo":
        return "Only photo captures can get another angle."
    if capture["status"] != "done":
        return "Only a finished capture can get another angle."
    return None


# ---------------------------------------------------------------- the group view (Story B)


@dataclass(frozen=True)
class Box:
    """One box of an item on one photo, as fractions of the upright display image."""

    photo_id: int
    idx: int
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass(frozen=True)
class GroupPhoto:
    id: int
    capture_id: int
    position: int  # within its capture
    index: int  # 0-based within the group (gallery order)
    width: int | None  # upright display size (None until the worker prepared it)
    height: int | None

    @property
    def url(self) -> str:
        return f"/photos/{self.id}"


@dataclass(frozen=True)
class GroupItem:
    """An item of the group's gallery. ``number`` (1-based) labels its boxes and list row.
    ``state``: "here" (live at the group's location), "moved" (live elsewhere: see
    ``location_name``/``location_code``) or "used_up"."""

    number: int
    id: int
    name: str
    quantity: int
    needs_review: bool
    capture_id: int | None
    state: str
    location_name: str
    location_code: str
    boxes: list[Box] = field(default_factory=list)

    @property
    def marked(self) -> bool:
        return bool(self.boxes)

    def boxes_on(self, photo_id: int) -> list[Box]:
        return [box for box in self.boxes if box.photo_id == photo_id]


@dataclass(frozen=True)
class Group:
    root: sqlite3.Row  # the root capture (captures.*)
    captures: list[sqlite3.Row]  # root first, then extensions (all statuses), oldest first
    photos: list[GroupPhoto]  # photos of the root and its done extensions, in order
    items: list[GroupItem]  # numbered; boxed items first (by first photo), then unmarked
    location_id: int | None  # the group's location (the root's)
    photo_capacity: int  # photos another angle may still add (12 minus counted photos)

    @property
    def root_id(self) -> int:
        return self.root["id"]

    def item(self, item_id: int) -> GroupItem | None:
        return next((entry for entry in self.items if entry.id == item_id), None)


def group(conn: sqlite3.Connection, capture_id: int) -> Group | None:
    """The group of any of its captures (root or extension), or None when the capture does
    not exist. Items are those created by the group's captures plus any item that has a box
    on one of its photos (e.g. an existing item matched by an extension), moved and used-up
    ones included with their state; items the user deleted (wrong detections,
    ``deleted_reason = 'deleted'``) are left out."""
    capture = conn.execute("SELECT * FROM captures WHERE id = ?", (capture_id,)).fetchone()
    if capture is None:
        return None
    root_capture_id = root_id(capture)
    root = conn.execute("SELECT * FROM captures WHERE id = ?", (root_capture_id,)).fetchone()
    captures = conn.execute(
        "SELECT * FROM captures WHERE id = ? OR parent_capture_id = ? ORDER BY id",
        (root_capture_id, root_capture_id),
    ).fetchall()
    done_ids = done_capture_ids(conn, root_capture_id)
    marks = ",".join("?" * len(done_ids))
    photo_rows = conn.execute(
        "SELECT id, capture_id, position, width, height FROM photos"
        f" WHERE capture_id IN ({marks})"
        " ORDER BY CASE WHEN capture_id = ? THEN 0 ELSE 1 END, capture_id, position, id",
        (*done_ids, root_capture_id),
    ).fetchall()
    photos = [
        GroupPhoto(r["id"], r["capture_id"], r["position"], i, r["width"], r["height"])
        for i, r in enumerate(photo_rows)
    ]
    photo_index = {photo.id: photo.index for photo in photos}
    boxes: dict[int, list[Box]] = {}
    if photos:
        photo_marks = ",".join("?" * len(photos))
        for r in conn.execute(
            "SELECT item_id, photo_id, idx, x0, y0, x1, y1 FROM item_boxes"
            f" WHERE photo_id IN ({photo_marks}) ORDER BY item_id, photo_id, idx",
            tuple(photo_index),
        ):
            boxes.setdefault(r["item_id"], []).append(
                Box(r["photo_id"], r["idx"], r["x0"], r["y0"], r["x1"], r["y1"])
            )
    item_ids = set(boxes)
    item_ids.update(
        row[0]
        for row in conn.execute(f"SELECT id FROM items WHERE capture_id IN ({marks})", done_ids)
    )
    items: list[GroupItem] = []
    if item_ids:
        id_marks = ",".join("?" * len(item_ids))
        rows = conn.execute(
            "SELECT i.id, i.name, i.quantity, i.needs_review, i.capture_id, i.location_id,"
            " i.deleted_at, l.name AS location_name, l.short_code AS location_code"
            " FROM items i JOIN locations l ON l.id = i.location_id"
            f" WHERE i.id IN ({id_marks}) AND i.deleted_reason IS NOT 'deleted'",
            tuple(item_ids),
        ).fetchall()

        def order(row: sqlite3.Row) -> tuple[int, float, int]:
            own = boxes.get(row["id"], [])
            if not own:
                return (len(photos) + 1, 0.0, row["id"])
            first = min(own, key=lambda b: (photo_index[b.photo_id], b.x0, b.y0))
            return (photo_index[first.photo_id], first.x0, row["id"])

        for number, row in enumerate(sorted(rows, key=order), start=1):
            if row["deleted_at"] is not None:
                state = "used_up"
            elif row["location_id"] != root["location_id"]:
                state = "moved"
            else:
                state = "here"
            items.append(
                GroupItem(
                    number=number,
                    id=row["id"],
                    name=row["name"],
                    quantity=row["quantity"],
                    needs_review=bool(row["needs_review"]),
                    capture_id=row["capture_id"],
                    state=state,
                    location_name=row["location_name"],
                    location_code=row["location_code"],
                    boxes=boxes.get(row["id"], []),
                )
            )
    capacity = MAX_GROUP_PHOTOS - len(counted_photos(conn, root_capture_id))
    return Group(
        root=root,
        captures=captures,
        photos=photos,
        items=items,
        location_id=root["location_id"],
        photo_capacity=max(capacity, 0),
    )


def extension_outcome(conn: sqlite3.Connection, capture_id: int) -> tuple[int, int]:
    """(new, already known) for a finished extension — "Added X new, Y already known": the
    items it created, and the live items it matched (``captures.known_count``, stored by its
    apply; boxed or not, gone items excluded). Extensions applied before that column count
    the other items that got (model) boxes from them."""
    row = conn.execute(
        "SELECT (SELECT count(*) FROM items WHERE capture_id = ?),"
        " coalesce((SELECT known_count FROM captures WHERE id = ?),"
        " (SELECT count(DISTINCT b.item_id) FROM item_boxes b JOIN items i ON i.id = b.item_id"
        "  WHERE b.capture_id = ? AND b.source = 'model' AND i.capture_id IS NOT ?))",
        (capture_id, capture_id, capture_id, capture_id),
    ).fetchone()
    return row[0], row[1]

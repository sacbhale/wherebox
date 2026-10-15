"""Item boxes repository (``item_boxes``, spec-multiphoto Part 3, R2, R16): where each item
was seen on which photo, as fractions (0..1) of the upright display image.

Writes run inside the capture apply ``write_tx``. Replacement is per photo: an item's rows are
deleted only on the photos where this call gave it at least one box, then the new ones are
inserted; on the call's other photos its older boxes stay (a model that leaves a box out has
not shown the item is absent there). Annotation data, not an item/location/ledger soft
delete. Reads serve the item page ("Where it was seen"), the review queue and item row crops,
the item photo view and the location page's photo.

A box the user drew ("Fix outline", ``source='user'``, spec-location-photo R4) replaces the
item's boxes on its photo in its own write_tx, and model boxes never replace it.
"""

import math
import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from app.ai.schemas import (
    MAX_BOXES_PER_IMAGE,
    ExtractedItem,
    ExtractResult,
    Fractions,
    boxes_for_images,
)
from app.capture.groups import Box
from app.capture.overview import DISPLAYABLE, overview
from app.db.connection import write_tx
from app.db.timestamps import NOW

# Review crop: the box grown by this fraction of its size on every side.
CROP_PADDING = 0.10
# A crop at least this wide/high shows the whole image dimension (no division by ~0).
FULL_EXTENT = 0.999
# Stored files browsers show as an image (HEIC originals and PDF receipts are not; a display
# JPEG, ``*.display.jpg``, is).
THUMBNAIL_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")

PhotoBox = tuple[int, Fractions]  # (photo id, box)


def photo_boxes(item: ExtractedItem, image_photo_ids: Sequence[int]) -> list[PhotoBox]:
    """The item's valid boxes mapped to the photos of the call (image index -> photo id)."""
    return [
        (image_photo_ids[box.image], box) for box in boxes_for_images(item, len(image_photo_ids))
    ]


def first_boxed_photo(boxes: Sequence[PhotoBox], image_photo_ids: Sequence[int]) -> int | None:
    """The first photo (in call order) with one of ``boxes``, else the call's first photo."""
    boxed = {photo_id for photo_id, _box in boxes}
    for photo_id in image_photo_ids:
        if photo_id in boxed:
            return photo_id
    return image_photo_ids[0] if image_photo_ids else None


def overview_photo(result: ExtractResult, image_photo_ids: Sequence[int]) -> int | None:
    """The call photo that shows the most of the place: the model's ``overview_image`` when
    it is the index of an image sent, else the photo with the most item boxes (first in call
    order on a tie), else the first photo; None without photos."""
    if not image_photo_ids:
        return None
    index = result.overview_image
    if isinstance(index, int) and 0 <= index < len(image_photo_ids):
        return image_photo_ids[index]
    counts = Counter(
        photo_id for item in result.items for photo_id, _box in photo_boxes(item, image_photo_ids)
    )
    return max(image_photo_ids, key=lambda photo_id: counts.get(photo_id, 0))


def record_overview(
    conn: sqlite3.Connection,
    capture_id: int,
    result: ExtractResult,
    image_photo_ids: Sequence[int],
) -> None:
    """Call inside the apply write_tx: ``captures.overview_photo_id`` = ``overview_photo``."""
    conn.execute(
        "UPDATE captures SET overview_photo_id = ? WHERE id = ?",
        (overview_photo(result, image_photo_ids), capture_id),
    )


def replace_boxes(
    conn: sqlite3.Connection,
    item_id: int,
    capture_id: int,
    boxes: Sequence[PhotoBox],
) -> int:
    """Call inside write_tx. Per photo: on each photo that has one of ``boxes``, the item's
    rows are replaced by those boxes (at most MAX_BOXES_PER_IMAGE per photo, in order); its
    rows on other photos are kept, so without boxes nothing changes. A photo where the item
    has a user box (``source='user'``, "Fix outline") is skipped: model boxes never replace
    it. Returns the rows inserted."""
    user_photos = {
        row[0]
        for row in conn.execute(
            "SELECT photo_id FROM item_boxes WHERE item_id = ? AND source = 'user'", (item_id,)
        )
    }
    boxes = [(photo_id, box) for photo_id, box in boxes if photo_id not in user_photos]
    if not boxes:
        return 0
    photo_ids = sorted({photo_id for photo_id, _box in boxes})
    marks = ",".join("?" * len(photo_ids))
    conn.execute(
        f"DELETE FROM item_boxes WHERE item_id = ? AND source = 'model' AND photo_id IN ({marks})",
        (item_id, *photo_ids),
    )
    counts: dict[int, int] = {}
    rows = []
    for photo_id, box in boxes:
        idx = counts.get(photo_id, 0)
        if idx >= MAX_BOXES_PER_IMAGE:
            continue
        counts[photo_id] = idx + 1
        rows.append((item_id, photo_id, capture_id, idx, box.x0, box.y0, box.x1, box.y1))
    conn.executemany(
        "INSERT INTO item_boxes (item_id, photo_id, capture_id, idx, x0, y0, x1, y1, created_at)"
        f" VALUES (?, ?, ?, ?, ?, ?, ?, ?, {NOW})",
        rows,
    )
    return len(rows)


def displayable(rel_path: str | None, display_rel_path: str | None) -> bool:
    """Whether /photos/{id} serves something a browser shows as an image (the review
    queue's thumbnail rule): the display JPEG, or a web-image original."""
    return (display_rel_path or rel_path or "").lower().endswith(THUMBNAIL_SUFFIXES)


# ---------------------------------------------------------------- reads (Story B)


@dataclass(frozen=True)
class Sighting:
    """One photo an item was seen on, with only that item's boxes (empty: the fallback
    ``items.photo_id`` photo, unmarked). ``location_name`` is the capture's location (where
    it was at the time), ``seen_at`` the capture's UTC timestamp."""

    photo_id: int
    width: int | None
    height: int | None
    capture_id: int | None
    root_capture_id: int | None
    location_name: str | None
    seen_at: str
    boxes: list[Box] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"/photos/{self.photo_id}"

    @property
    def marked(self) -> bool:
        return bool(self.boxes)


_PHOTO_CONTEXT = (
    "SELECT p.id, p.width, p.height, p.capture_id, p.created_at AS photo_created_at,"
    " c.created_at AS capture_created_at, coalesce(c.parent_capture_id, c.id) AS root_id,"
    " l.name AS location_name"
    " FROM photos p LEFT JOIN captures c ON c.id = p.capture_id"
    " LEFT JOIN locations l ON l.id = c.location_id"
)


def sightings(conn: sqlite3.Connection, item_id: int) -> list[Sighting]:
    """Where the item was seen: every photo with a box for it (oldest capture first, photo
    position order), each with only this item's boxes. Without boxes, the item's own
    ``photo_id`` photo unmarked when a browser can show it (``displayable``: not a PDF
    receipt); [] otherwise."""
    rows = conn.execute(
        "SELECT b.photo_id, b.idx, b.x0, b.y0, b.x1, b.y1 FROM item_boxes b"
        " JOIN photos p ON p.id = b.photo_id WHERE b.item_id = ?"
        " ORDER BY p.capture_id, p.position, p.id, b.idx",
        (item_id,),
    ).fetchall()
    by_photo: dict[int, list[Box]] = {}
    for r in rows:
        by_photo.setdefault(r["photo_id"], []).append(
            Box(r["photo_id"], r["idx"], r["x0"], r["y0"], r["x1"], r["y1"])
        )
    photo_ids = list(by_photo)
    if not photo_ids:
        own = conn.execute(
            "SELECT p.id, p.rel_path, p.display_rel_path FROM items i"
            " JOIN photos p ON p.id = i.photo_id WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        if own is None or not displayable(own["rel_path"], own["display_rel_path"]):
            return []
        photo_ids = [own["id"]]
    return _with_context(conn, photo_ids, by_photo)


def _with_context(
    conn: sqlite3.Connection, photo_ids: list[int], by_photo: dict[int, list[Box]]
) -> list[Sighting]:
    """Sightings of ``photo_ids`` (in that order; unknown ids left out) with ``by_photo``'s
    boxes."""
    context = {
        r["id"]: r
        for chunk in _chunks(photo_ids)
        for r in conn.execute(
            _PHOTO_CONTEXT + f" WHERE p.id IN ({','.join('?' * len(chunk))})", tuple(chunk)
        )
    }
    return [
        Sighting(
            photo_id=photo_id,
            width=context[photo_id]["width"],
            height=context[photo_id]["height"],
            capture_id=context[photo_id]["capture_id"],
            root_capture_id=context[photo_id]["root_id"],
            location_name=context[photo_id]["location_name"],
            seen_at=context[photo_id]["capture_created_at"]
            or context[photo_id]["photo_created_at"],
            boxes=by_photo.get(photo_id, []),
        )
        for photo_id in photo_ids
        if photo_id in context
    ]


@dataclass(frozen=True)
class Crop:
    """The crop of an item (review queue, item rows): its preferred box (``review_crops``),
    padded by CROP_PADDING and squared (``crop_region``).
    ``background_size``/``background_position`` are CSS values (numbers only) that show
    that region of the display photo in a square element (``background-image: url(<url>)``)
    without stretching it."""

    item_id: int
    photo_id: int
    capture_id: int
    root_capture_id: int
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def url(self) -> str:
        return f"/photos/{self.photo_id}"

    @staticmethod
    def _axis(start: float, end: float) -> tuple[float, float]:
        extent = end - start
        if extent >= FULL_EXTENT:
            return 100.0, 0.0
        return 100.0 / extent, min(max(start / (1.0 - extent) * 100.0, 0.0), 100.0)

    @property
    def background_size(self) -> str:
        width = self._axis(self.x0, self.x1)[0]
        height = self._axis(self.y0, self.y1)[0]
        return f"{width:.2f}% {height:.2f}%"

    @property
    def background_position(self) -> str:
        left = self._axis(self.x0, self.x1)[1]
        top = self._axis(self.y0, self.y1)[1]
        return f"{left:.2f}% {top:.2f}%"


def _padded(start: float, end: float) -> tuple[float, float]:
    pad = (end - start) * CROP_PADDING
    return max(start - pad, 0.0), min(end + pad, 1.0)


def _square_axis(centre: float, extent: float) -> tuple[float, float]:
    """An interval of ``extent`` (capped at 1) centred on ``centre``, shifted back inside
    [0, 1] when it sticks out (shifted, not clipped, so it keeps its size)."""
    extent = min(extent, 1.0)
    low = min(max(centre - extent / 2, 0.0), 1.0 - extent)
    return low, low + extent


def crop_region(
    x0: float, y0: float, x1: float, y1: float, width: int | None, height: int | None
) -> tuple[float, float, float, float]:
    """The review crop of a box (fractions). With the display size (``photos.width/height``,
    upright): the box padded by CROP_PADDING, its shorter side grown in pixels to a square
    (capped at the whole image), centred on the box and shifted inside the image, so a
    square element shows it unstretched. Without a size: the padded box clamped to the
    image (as before sizes were recorded)."""
    if not width or not height or width <= 0 or height <= 0:
        px0, px1 = _padded(x0, x1)
        py0, py1 = _padded(y0, y1)
        return px0, py0, px1, py1
    grow = 1 + 2 * CROP_PADDING
    side = max((x1 - x0) * grow * width, (y1 - y0) * grow * height)
    cx0, cx1 = _square_axis((x0 + x1) / 2, side / width)
    cy0, cy1 = _square_axis((y0 + y1) / 2, side / height)
    return cx0, cy0, cx1, cy1


IN_CHUNK = 500  # ids per ``IN (...)`` list, far below SQLite's variable limit


def _chunks(ids: Sequence[int]) -> Iterable[Sequence[int]]:
    for start in range(0, len(ids), IN_CHUNK):
        yield ids[start : start + IN_CHUNK]


def review_crops(conn: sqlite3.Connection, item_ids: Iterable[int]) -> dict[int, Crop]:
    """{item id: crop} for the given items that have a box (items without one are absent),
    in one query. The preferred box: the latest user box ("Fix outline"), else a box of the
    newest capture that boxed the item (a moved or reconciled item shows its latest
    sighting), first by photo position."""
    ids = list(dict.fromkeys(item_ids))
    if not ids:
        return {}
    crops: dict[int, Crop] = {}
    for chunk in _chunks(ids):
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            "SELECT b.item_id, b.photo_id, b.x0, b.y0, b.x1, b.y1, p.capture_id, p.width,"
            " p.height, coalesce(c.parent_capture_id, c.id) AS root_id"
            " FROM item_boxes b JOIN photos p ON p.id = b.photo_id"
            " JOIN captures c ON c.id = p.capture_id"
            f" WHERE b.item_id IN ({marks})"
            " ORDER BY b.item_id, b.source = 'user' DESC,"
            " CASE WHEN b.source = 'user' THEN b.id END DESC, b.capture_id DESC, p.position,"
            " p.id, b.idx",
            tuple(chunk),
        ).fetchall()
        for r in rows:
            if r["item_id"] in crops:
                continue
            region = crop_region(r["x0"], r["y0"], r["x1"], r["y1"], r["width"], r["height"])
            crops[r["item_id"]] = Crop(
                r["item_id"], r["photo_id"], r["capture_id"], r["root_id"], *region
            )
    return crops


# ---------------------------------------------------------------- user boxes ("Fix outline")

# The smallest outline side a user may draw, as a fraction of the photo (data-min in the page).
MIN_USER_BOX = 0.02


class BoxError(ValueError):
    """A rejected outline (HTTP 400)."""


def parse_user_box(values: Mapping[str, str]) -> tuple[float, float, float, float]:
    """(x0, y0, x1, y1) from form strings: finite numbers with 0 <= x0 < x1 <= 1 and
    0 <= y0 < y1 <= 1, each side at least MIN_USER_BOX. Raises BoxError."""
    coords = []
    for key in ("x0", "y0", "x1", "y1"):
        try:
            value = float((values.get(key) or "").strip())
        except ValueError as exc:
            raise BoxError("The outline needs four numbers.") from exc
        if not math.isfinite(value):
            raise BoxError("The outline needs four numbers.")
        coords.append(value)
    x0, y0, x1, y1 = coords
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        raise BoxError("The outline must be inside the photo.")
    # The page posts values rounded to 6 decimals, so a side drawn at exactly the minimum can
    # arrive a hair under it.
    if x1 - x0 < MIN_USER_BOX - 1e-6 or y1 - y0 < MIN_USER_BOX - 1e-6:
        raise BoxError("The outline is too small; draw a bigger one.")
    return x0, y0, x1, y1


def set_user_box(
    conn: sqlite3.Connection,
    item_id: int,
    photo_id: int,
    coords: tuple[float, float, float, float],
) -> None:
    """ "Fix outline": the item's boxes on ``photo_id`` are replaced by one ``source='user'``
    box (idx 0, ``capture_id`` = the photo's capture), in one write_tx. Raises BoxError for
    a photo without a capture."""
    with write_tx(conn):
        photo = conn.execute("SELECT capture_id FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if photo is None or photo["capture_id"] is None:
            raise BoxError("This photo cannot be outlined.")
        conn.execute(
            "DELETE FROM item_boxes WHERE item_id = ? AND photo_id = ?", (item_id, photo_id)
        )
        conn.execute(
            "INSERT INTO item_boxes (item_id, photo_id, capture_id, idx, x0, y0, x1, y1, source,"
            f" created_at) VALUES (?, ?, ?, 0, ?, ?, ?, ?, 'user', {NOW})",
            (item_id, photo_id, photo["capture_id"], *coords),
        )


def photo_choices(conn: sqlite3.Connection, item_id: int) -> list[Sighting]:
    """The photos the item photo view offers (and ``POST /items/{id}/box`` accepts): where
    it was seen (``sightings``), plus its current location's photo (``overview.overview``),
    each with a display JPEG and a display size."""
    item = conn.execute("SELECT location_id FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        return []
    choices = sightings(conn, item_id)
    location = conn.execute(
        "SELECT * FROM locations WHERE id = ? AND deleted_at IS NULL", (item["location_id"],)
    ).fetchone()
    view = overview(conn, location) if location is not None else None
    if view is not None and view.photo_id not in {s.photo_id for s in choices}:
        choices += _with_context(conn, [view.photo_id], {})
    if not choices:
        return []
    shown = {
        row[0]
        for chunk in _chunks([s.photo_id for s in choices])
        for row in conn.execute(
            f"SELECT p.id FROM photos p WHERE p.id IN ({','.join('?' * len(chunk))})"
            f" AND {DISPLAYABLE}",
            tuple(chunk),
        )
    }
    return [s for s in choices if s.photo_id in shown]


def location_boxes(
    conn: sqlite3.Connection, photo_id: int, location_id: int
) -> dict[int, list[Box]]:
    """{item id: its boxes on ``photo_id``} for the live items directly in ``location_id``
    (the location page's photo)."""
    found: dict[int, list[Box]] = {}
    for r in conn.execute(
        "SELECT b.item_id, b.photo_id, b.idx, b.x0, b.y0, b.x1, b.y1 FROM item_boxes b"
        " JOIN items i ON i.id = b.item_id"
        " WHERE b.photo_id = ? AND i.location_id = ? AND i.deleted_at IS NULL"
        " ORDER BY b.item_id, b.idx",
        (photo_id, location_id),
    ):
        found.setdefault(r["item_id"], []).append(
            Box(r["photo_id"], r["idx"], r["x0"], r["y0"], r["x1"], r["y1"])
        )
    return found

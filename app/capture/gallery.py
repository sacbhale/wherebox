"""View model of the annotated photo gallery (spec-multiphoto Part 5, R14-R16): photos
("frames") with the boxes ("marks") drawn on them, for the one gallery partial used by the
capture detail page, the capture panel's result, the item page and the comparison report.

Pure and read-only. Every value that reaches a ``style`` attribute is a number formatted
here (percentages of the upright display image), never text from a model."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.capture.boxes import Sighting
from app.capture.groups import Box, Group, GroupPhoto
from app.capture.overview import Overview

# A label is anchored to the box's right / bottom edge instead of its top-left corner when
# the box starts this far right / down, so it stays inside the image.
LABEL_END_X = 0.8
LABEL_UP_Y = 0.85


def _fraction(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return min(max(number, 0.0), 1.0)


@dataclass(frozen=True)
class Mark:
    """One box on a frame. ``key`` ties it to its list row (same key = same entry);
    ``number`` is its label (None: unlabelled); ``model`` is the report's model index."""

    key: str
    number: int | None
    x0: float
    y0: float
    x1: float
    y1: float
    active: bool = False
    model: int | None = None

    @property
    def style(self) -> str:
        return (
            f"left:{self.x0 * 100:.2f}%;top:{self.y0 * 100:.2f}%;"
            f"width:{(self.x1 - self.x0) * 100:.2f}%;height:{(self.y1 - self.y0) * 100:.2f}%"
        )

    @property
    def label_class(self) -> str:
        classes = []
        if self.x0 > LABEL_END_X:
            classes.append("label-end")
        if self.y0 > LABEL_UP_Y:
            classes.append("label-up")
        return " ".join(classes)


def mark(key: str, number: int | None, box: Any, **extra: Any) -> Mark | None:
    """A mark from anything with x0..y1 (attributes or keys); None when not a valid box."""
    get = box.get if isinstance(box, Mapping) else lambda name: getattr(box, name, None)
    x0, y0, x1, y1 = (_fraction(get(name)) for name in ("x0", "y0", "x1", "y1"))
    if x0 is None or y0 is None or x1 is None or y1 is None or x1 <= x0 or y1 <= y0:
        return None
    return Mark(key, number, x0, y0, x1, y1, **extra)


@dataclass(frozen=True)
class Frame:
    photo_id: int
    width: int | None
    height: int | None
    marks: list[Mark] = field(default_factory=list)

    @property
    def url(self) -> str:
        return f"/photos/{self.photo_id}"

    @property
    def ratio_style(self) -> str:
        """``aspect-ratio`` from the display size (reserves the space before the image
        loads), or "" when the size is unknown."""
        width, height = self.width, self.height
        if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
            return f"aspect-ratio:{width}/{height}"
        return ""


def group_frames(group: Group, highlight: int | None = None) -> list[Frame]:
    """The group's photos with every item's boxes, numbered like the item list;
    ``highlight`` (an item id) starts highlighted."""
    frames = []
    for photo in group.photos:
        marks = []
        for entry in group.items:
            for box in entry.boxes_on(photo.id):
                made = mark(str(entry.id), entry.number, box, active=entry.id == highlight)
                if made is not None:
                    marks.append(made)
        frames.append(Frame(photo.id, photo.width, photo.height, marks))
    return frames


def sighting_frames(sightings: Iterable[Sighting]) -> list[Frame]:
    """The item page's photos, each with only this item's boxes (highlighted, unlabelled)."""
    frames = []
    for sighting in sightings:
        marks = [
            made
            for box in sighting.boxes
            if (made := mark("item", None, box, active=True)) is not None
        ]
        frames.append(Frame(sighting.photo_id, sighting.width, sighting.height, marks))
    return frames


def overview_frames(view: Overview, item_boxes: Mapping[int, list[Box]]) -> list[Frame]:
    """The location page's photo with its live direct items' boxes, unlabelled and keyed by
    item id (``item_boxes``: ``boxes.location_boxes``)."""
    marks = [
        made
        for item_id, found in item_boxes.items()
        for box in found
        if (made := mark(str(item_id), None, box)) is not None
    ]
    return [Frame(view.photo_id, view.width, view.height, marks)]


def report_frames(photos: Sequence[GroupPhoto], models: Sequence[Any]) -> list[Frame]:
    """A comparison case's photos with each model's boxes (``model`` = its index in
    ``models``), numbered by prediction index; only the first model's are visible at first."""
    frames = []
    for photo in photos:
        marks = []
        for index, model_case in enumerate(models):
            for line in model_case.lines:
                if line.number is None:
                    continue
                for box in line.boxes:
                    if not isinstance(box, Mapping) or box.get("photo_id") != photo.id:
                        continue
                    made = mark(f"{index}-{line.number}", line.number, box, model=index)
                    if made is not None:
                        marks.append(made)
        frames.append(Frame(photo.id, photo.width, photo.height, marks))
    return frames

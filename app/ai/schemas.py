"""Provider-neutral request/result types and the JSON schema of the forced extraction tool."""

import copy
import logging
import math
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

log = logging.getLogger(__name__)

# "extend": an "add another angle" capture (all photos of a capture group, existing items).
Mode = Literal["shelf", "receipt", "text", "reconcile", "extend"]
Kind = Literal["consumable", "belonging"]

# Modes whose answers carry boxes (one entry per object per view) and so get the larger
# output cap. Receipts and text keep DEFAULT_OUTPUT_TOKENS.
BOX_MODES: frozenset[str] = frozenset({"shelf", "reconcile", "extend"})
DEFAULT_OUTPUT_TOKENS = 4096
BOX_OUTPUT_TOKENS = 16_384
# Boxes use Gemini's convention: [ymin, xmin, ymax, xmax] integers 0..1000 of each image.
BOX_SCALE = 1000
MAX_BOXES_PER_IMAGE = 4


def output_cap(mode: str, default: int = DEFAULT_OUTPUT_TOKENS) -> int:
    """The ``max_tokens`` of one request: ``default``, raised to BOX_OUTPUT_TOKENS for the
    photo modes (a many-item shelf with boxes in several views is long)."""
    return max(default, BOX_OUTPUT_TOKENS) if mode in BOX_MODES else default


class ExistingItem(BaseModel):
    id: int
    name: str
    quantity: int
    # Extensions: a group item that was moved elsewhere or used up since the older photos
    # were taken ("not here any more — do not re-add"); it may be matched, never re-added.
    gone: bool = False


class ExtractRequest(BaseModel):
    mode: Mode
    images: list[bytes] = Field(default_factory=list)  # JPEG, long edge <= AI_MAX_IMAGE_EDGE
    text: str | None = None
    known_locations: list[str] = Field(default_factory=list)
    existing_items: list[ExistingItem] = Field(default_factory=list)
    timeout_s: float = Field(default=60, gt=0)


class ExtractedItem(BaseModel):
    name: str = Field(min_length=1)
    quantity: int = Field(ge=1)
    category: str | None = None
    kind: Kind
    confidence: float = Field(ge=0, le=1)
    location_hint: str | None = None
    price_cents: int | None = Field(default=None, ge=0)
    purchase_date: date | None = None
    matched_existing_id: int | None = None
    # [{"image": i, "box_2d": [ymin, xmin, ymax, xmax]}], cleaned by ``clean_boxes`` (never
    # raises: invalid entries are dropped; the image index is checked against the request
    # later, by ``boxes_for_images``).
    boxes: list[Any] | None = None

    @field_validator("boxes", mode="before")
    @classmethod
    def _clean_boxes(cls, value: Any) -> list[dict[str, Any]] | None:
        return clean_boxes(value)


class ReceiptMeta(BaseModel):
    merchant: str | None = None
    purchase_date: date | None = None


class Usage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    # What the provider charged for the call in USD, when it reports it (OpenRouter does).
    # The ledger prefers it over token prices (budget.cost_micros_for).
    cost_usd: float | None = Field(default=None, ge=0)


def clean_overview_image(value: Any) -> int | None:
    """The photo modes' ``overview_image`` (the index of the image that shows the most of
    the place): a non-bool int >= 0, else None. Never raises; the index is checked against
    the number of images sent at apply time."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class ToolOutput(BaseModel):
    """What the model returns through the forced tool call (validated by adapters)."""

    model_config = ConfigDict(extra="ignore")

    items: list[ExtractedItem]
    receipt: ReceiptMeta | None = None
    overview_image: Any = None

    @field_validator("overview_image", mode="before")
    @classmethod
    def _clean_overview(cls, value: Any) -> int | None:
        return clean_overview_image(value)


class ExtractResult(BaseModel):
    items: list[ExtractedItem]
    receipt: ReceiptMeta | None = None
    overview_image: Any = None
    usage: Usage

    @field_validator("overview_image", mode="before")
    @classmethod
    def _clean_overview(cls, value: Any) -> int | None:
        return clean_overview_image(value)


# ---------------------------------------------------------------- boxes


def _coordinate(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(value):
        return None
    return min(max(round(value), 0), BOX_SCALE)


def clean_boxes(value: Any, n_images: int | None = None) -> list[dict[str, Any]] | None:
    """Keep the valid box entries of a model answer: ``{"image": int >= 0 (< n_images when
    given), "box_2d": 4 numbers}`` with each number clamped to 0..1000 and rounded, a
    positive area (ymin < ymax, xmin < xmax), at most MAX_BOXES_PER_IMAGE per image. Wrong
    shapes, strings, bools, NaN, bad indexes are dropped (debug log). Never raises; None
    when ``value`` is not a list."""
    if value is None:
        return None
    if not isinstance(value, list):
        log.debug("boxes dropped: not a list (%s)", type(value).__name__)
        return None
    kept: list[dict[str, Any]] = []
    per_image: dict[int, int] = {}
    for entry in value:
        box = entry.get("box_2d") if isinstance(entry, dict) else None
        image = entry.get("image") if isinstance(entry, dict) else None
        if (
            isinstance(image, bool)
            or not isinstance(image, int)
            or image < 0
            or (n_images is not None and image >= n_images)
            or not isinstance(box, list | tuple)
            or len(box) != 4
        ):
            log.debug("box dropped: bad shape or image index: %.200r", entry)
            continue
        coords = [_coordinate(v) for v in box]
        if any(c is None for c in coords):
            log.debug("box dropped: non-numeric coordinate: %.200r", entry)
            continue
        ymin, xmin, ymax, xmax = coords  # type: ignore[misc]
        if not (ymin < ymax and xmin < xmax):  # type: ignore[operator]
            log.debug("box dropped: empty area: %.200r", entry)
            continue
        if per_image.get(image, 0) >= MAX_BOXES_PER_IMAGE:
            log.debug("box dropped: more than %d boxes in image %d", MAX_BOXES_PER_IMAGE, image)
            continue
        per_image[image] = per_image.get(image, 0) + 1
        kept.append({"image": image, "box_2d": [ymin, xmin, ymax, xmax]})
    return kept


@dataclass(frozen=True)
class Fractions:
    """A box as fractions of the upright image: 0 <= x0 < x1 <= 1, 0 <= y0 < y1 <= 1."""

    image: int
    x0: float
    y0: float
    x1: float
    y1: float


def to_fractions(entry: dict[str, Any]) -> Fractions:
    """A cleaned ``{"image", "box_2d": [ymin, xmin, ymax, xmax]}`` entry as fractions."""
    ymin, xmin, ymax, xmax = entry["box_2d"]
    return Fractions(
        entry["image"], xmin / BOX_SCALE, ymin / BOX_SCALE, xmax / BOX_SCALE, ymax / BOX_SCALE
    )


def boxes_for_images(item: "ExtractedItem", n_images: int) -> list[Fractions]:
    """The item's boxes whose image index is one of the ``n_images`` sent, as fractions
    (the 0..1000 values of an image map 1:1 onto its display JPEG: both are the upright
    original scaled with the same aspect ratio)."""
    return [to_fractions(entry) for entry in clean_boxes(item.boxes, n_images) or []]


# ---------------------------------------------------------------- tool schema

TOOL_NAME = "record_items"
TOOL_DESCRIPTION = "Record every household item found in the input."

_NULLABLE_STRING: dict[str, Any] = {"type": ["string", "null"]}
_NULLABLE_DATE: dict[str, Any] = {
    "type": ["string", "null"],
    "description": "ISO date YYYY-MM-DD, or null when unknown.",
}

# Input schema of the forced tool. Kept inline (no $ref) so every provider accepts it;
# test_provider_contract checks it stays in step with ToolOutput.
TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short product or object name."},
                    "quantity": {"type": "integer", "minimum": 1},
                    "category": _NULLABLE_STRING,
                    "kind": {"type": "string", "enum": ["consumable", "belonging"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "location_hint": _NULLABLE_STRING,
                    "price_cents": {"type": ["integer", "null"], "minimum": 0},
                    "purchase_date": _NULLABLE_DATE,
                    "matched_existing_id": {"type": ["integer", "null"]},
                    "boxes": {
                        "type": ["array", "null"],
                        "description": (
                            "Where the item is in each image it is visible in: image = the "
                            "0-based image index, box_2d = [ymin, xmin, ymax, xmax] as integers "
                            "0-1000 of that image's height and width. Null when not marked."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "image": {"type": "integer", "minimum": 0},
                                "box_2d": {
                                    "type": "array",
                                    "items": {"type": "integer", "minimum": 0, "maximum": 1000},
                                    "minItems": 4,
                                    "maxItems": 4,
                                },
                            },
                            "required": ["image", "box_2d"],
                        },
                    },
                },
                "required": ["name", "quantity", "kind", "confidence"],
            },
        },
        "receipt": {
            "type": ["object", "null"],
            "properties": {"merchant": _NULLABLE_STRING, "purchase_date": _NULLABLE_DATE},
        },
        "overview_image": {
            "type": ["integer", "null"],
            "description": (
                "The 0-based index of the image that shows the most of the place (the widest, "
                "most zoomed-out view). Null for receipts and text."
            ),
        },
    },
    "required": ["items"],
}

# JSON-schema keywords that strict structured-output modes reject; ToolOutput still enforces them.
_NUMERIC_BOUNDS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minItems",
    "maxItems",
)


def _strictify(node: dict[str, Any]) -> None:
    for key in _NUMERIC_BOUNDS:
        node.pop(key, None)
    properties = node.get("properties")
    if isinstance(properties, dict):
        # Strict mode: every property is required (optional ones are already nullable)
        # and no other properties are allowed.
        node["required"] = list(properties)
        node["additionalProperties"] = False
        for child in properties.values():
            _strictify(child)
    items = node.get("items")
    if isinstance(items, dict):
        _strictify(items)


def strict_json_schema() -> dict[str, Any]:
    """TOOL_INPUT_SCHEMA for ``response_format: json_schema`` with ``strict: true``: every
    object lists all its properties as required, sets ``additionalProperties: false``, and
    numeric and array-length bounds are dropped. A fresh copy on every call."""
    schema = copy.deepcopy(TOOL_INPUT_SCHEMA)
    _strictify(schema)
    return schema

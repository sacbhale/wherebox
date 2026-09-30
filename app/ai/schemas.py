"""Provider-neutral request/result types and the JSON schema of the forced extraction tool."""

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Mode = Literal["shelf", "receipt", "text", "reconcile"]
Kind = Literal["consumable", "belonging"]


class ExistingItem(BaseModel):
    id: int
    name: str
    quantity: int


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


class ReceiptMeta(BaseModel):
    merchant: str | None = None
    purchase_date: date | None = None


class Usage(BaseModel):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class ToolOutput(BaseModel):
    """What the model returns through the forced tool call (validated by adapters)."""

    model_config = ConfigDict(extra="ignore")

    items: list[ExtractedItem]
    receipt: ReceiptMeta | None = None


class ExtractResult(BaseModel):
    items: list[ExtractedItem]
    receipt: ReceiptMeta | None = None
    usage: Usage


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
                },
                "required": ["name", "quantity", "kind", "confidence"],
            },
        },
        "receipt": {
            "type": ["object", "null"],
            "properties": {"merchant": _NULLABLE_STRING, "purchase_date": _NULLABLE_DATE},
        },
    },
    "required": ["items"],
}

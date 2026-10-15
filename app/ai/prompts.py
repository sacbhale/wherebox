"""Provider-neutral prompts, one per capture mode. Every mode answers via the forced tool,
or, for models without tool calling, with the tool's input as a JSON object (``json_answer``)."""

import json

from app.ai.schemas import BOX_MODES, TOOL_INPUT_SCHEMA, TOOL_NAME, ExtractRequest, Mode

_COMMON = f"""You catalogue a household inventory. Always answer by calling the `{TOOL_NAME}` tool
exactly once. For every item give: a short generic name (brand only when it matters),
quantity as a whole number >= 1, a simple category (e.g. Food, Cleaning, Tools, Electronics),
kind ("consumable" for things that get used up, "belonging" for durable things), and a
confidence between 0 and 1 for how sure you are about the name and quantity.
Leave fields you cannot determine as null. Never invent items that are not there."""

# Shared by the photo modes (shelf, reconcile, extend): several views and the boxes.
_VIEWS_AND_BOXES = """Each image is preceded by a label "Image N:" (N = 0, 1, 2, ... in the order
given). When there are several images, they are different views (angles) of the same place:
list each physical object only once, even when it is visible in several images, and count
identical objects across all views without counting the same object twice.
For every item, set boxes to where it is visible: one entry per box, {"image": N, "box_2d":
[ymin, xmin, ymax, xmax]}, where N is the 0-based image index and the four numbers are
integers from 0 to 1000 relative to that image (0,0 = top-left corner; y is measured down the
height, x across the width). Give a box in every image the item appears in, at most 4 boxes
per item per image (e.g. for separate identical objects). Set boxes to null when unsure.
Set overview_image to the index N of the image that shows the most of the place (the widest,
most zoomed-out view), or null when unsure."""

_MODE_RULES: dict[Mode, str] = {
    "shelf": """The images show a shelf, drawer, box or room. List every distinct item you can
see. Count identical items together as one entry with the right quantity. Use a lower
confidence for items that are partly hidden, blurry or hard to identify.
Set location_hint, price_cents, purchase_date and matched_existing_id to null.""",
    "receipt": """The input is a shopping receipt. Record product lines only: one entry per
purchased product, with quantity and the line's price in cents (price_cents, the line total
for that product). Do NOT record tax, VAT, total, subtotal, bag or bag fees, bottle deposits,
discounts, coupons, change, cash tendered, card payments, tips, loyalty points or store
information as items. Put the store name in receipt.merchant and the receipt date in
receipt.purchase_date (YYYY-MM-DD). Set an item's purchase_date only when its line has its
own date. Set location_hint and matched_existing_id to null.""",
    "text": """The input is a sentence typed or dictated by a household member describing items
and where they are. Record each item mentioned with its quantity (default 1 when not said).
When the text says where an item is, set location_hint to the matching name from the
known locations list, spelled exactly as listed; if no listed location matches, set it to
the place name as written. Otherwise set location_hint to null.""",
    "reconcile": """The images show a location whose recorded contents are listed as existing
items (id, name, quantity). List every item you can see. When a seen item is one of the
existing items, set matched_existing_id to that item's id and quantity to the count you see;
never use an id that is not in the list. Items that are not in the list get
matched_existing_id null. Do not list existing items that you cannot see.""",
    "extend": """All images are views of the same place: the first ones were catalogued
before, the last ones are new angles. Its recorded contents are listed as existing items
(id, name, quantity). List every physical object you see, once across all views. When an
object is one of the existing items, set matched_existing_id to that item's id and quantity
to the count you see across all views; never use an id that is not in the list. Objects not
in the list get matched_existing_id null. Existing items marked
"not here any more — do not re-add" were moved or used up after the older photos were taken:
if you see one, set matched_existing_id to its id, and never list it as a new item.""",
}


def system_prompt(mode: Mode) -> str:
    prompt = f"{_COMMON}\n\n{_MODE_RULES[mode]}"
    if mode in BOX_MODES:
        return f"{prompt}\n\n{_VIEWS_AND_BOXES}"
    return f"{prompt}\nSet boxes to null. Set overview_image to null."


def image_label(index: int) -> str:
    """The text part both adapters send right before image ``index`` (0-based)."""
    return f"Image {index}:"


GONE_TAG = "not here any more — do not re-add"


def user_text(req: ExtractRequest) -> str:
    """The text part of the user turn (images, if any, are sent alongside it)."""
    parts: list[str] = []
    if req.mode == "text":
        locations = ", ".join(req.known_locations) or "(none)"
        parts.append(f"Known locations: {locations}")
        parts.append(f"Text: {req.text or ''}")
    elif req.mode in ("reconcile", "extend"):
        if req.existing_items:
            lines = "\n".join(
                f"- id={item.id}: {item.name} (quantity {item.quantity})"
                + (f" — {GONE_TAG}" if item.gone else "")
                for item in req.existing_items
            )
        else:
            lines = "(none)"
        parts.append(f"Existing items:\n{lines}")
    elif req.mode == "receipt":
        parts.append("Extract the purchased products from this receipt.")
    else:
        parts.append("Catalogue the items in these images.")
    if req.mode in BOX_MODES and req.images:
        count = len(req.images)
        parts.append(
            "There is 1 image (Image 0)."
            if count == 1
            else f"There are {count} images (Image 0 to Image {count - 1})."
        )
    if req.text and req.mode != "text":
        parts.append(f"Note from the user: {req.text}")
    return "\n\n".join(parts)


def json_answer(*, include_schema: bool) -> str:
    """Appended to the system prompt when the model answers with JSON text instead of a tool
    call. ``include_schema`` spells the schema out (when no response_format enforces it)."""
    note = (
        f"No tools are available here. Instead of calling `{TOOL_NAME}`, reply with only the "
        "JSON object you would pass to it: no prose before or after it."
    )
    if include_schema:
        note += "\nThe JSON object must match this JSON schema:\n" + json.dumps(TOOL_INPUT_SCHEMA)
    return note

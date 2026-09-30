"""Provider-neutral prompts, one per capture mode. Every mode answers via the forced tool."""

from app.ai.schemas import TOOL_NAME, ExtractRequest, Mode

_COMMON = f"""You catalogue a household inventory. Always answer by calling the `{TOOL_NAME}` tool
exactly once. For every item give: a short generic name (brand only when it matters),
quantity as a whole number >= 1, a simple category (e.g. Food, Cleaning, Tools, Electronics),
kind ("consumable" for things that get used up, "belonging" for durable things), and a
confidence between 0 and 1 for how sure you are about the name and quantity.
Leave fields you cannot determine as null. Never invent items that are not there."""

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
}


def system_prompt(mode: Mode) -> str:
    return f"{_COMMON}\n\n{_MODE_RULES[mode]}"


def user_text(req: ExtractRequest) -> str:
    """The text part of the user turn (images, if any, are sent alongside it)."""
    parts: list[str] = []
    if req.mode == "text":
        locations = ", ".join(req.known_locations) or "(none)"
        parts.append(f"Known locations: {locations}")
        parts.append(f"Text: {req.text or ''}")
    elif req.mode == "reconcile":
        if req.existing_items:
            lines = "\n".join(
                f"- id={item.id}: {item.name} (quantity {item.quantity})"
                for item in req.existing_items
            )
        else:
            lines = "(none)"
        parts.append(f"Existing items:\n{lines}")
    elif req.mode == "receipt":
        parts.append("Extract the purchased products from this receipt.")
    else:
        parts.append("Catalogue the items in these images.")
    if req.text and req.mode != "text":
        parts.append(f"Note from the user: {req.text}")
    return "\n\n".join(parts)

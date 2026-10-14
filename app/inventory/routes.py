"""Inventory pages: home, search, location pages (URL, QR, label), location and item forms,
and the htmx quick actions. Access control comes from the global auth middleware; unsafe
methods are origin-checked there too. No state changes over GET."""

import sqlite3
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from app import label_sheet, qr
from app.auth import sessions
from app.auth.csrf import is_htmx, require_htmx
from app.capture import boxes
from app.config import Settings
from app.db.deps import Conn
from app.inventory import categories, items, locations, search
from app.templating import templates

router = APIRouter()

FormFields = dict[str, str]


async def form_fields(request: Request) -> FormFields:
    """The submitted form as plain strings (lets sync handlers take a whole form)."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


Form = Annotated[FormFields, Depends(form_fields)]
HTMX = [Depends(require_htmx)]


def permanent_url(settings: Settings, code: str) -> str:
    """The URL printed on tags: always PUBLIC_BASE_URL, never the request's Host."""
    return f"{settings.public_base_url}/l/{code}"


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _redirect(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _message(request: Request, title: str, message: str, status: int) -> Response:
    return templates.TemplateResponse(
        request, "message.html", {"title": title, "message": message}, status_code=status
    )


def _resolve(
    request: Request, conn: sqlite3.Connection, code: str, pending_move: bool = False
) -> sqlite3.Row | Response:
    """A live location for ``code``, or the 410 (removed) / 404 (unknown) response.
    ``pending_move`` makes the 410 page say a scan-to-move is still waiting."""
    status, row = locations.lookup_code(conn, code)
    if status == "live":
        return row
    if status == "deleted":
        return templates.TemplateResponse(
            request,
            "location_removed.html",
            {"code": row["short_code"], "pending_move": pending_move},
            status_code=410,
        )
    return _message(request, "Not found", "No location has this code.", 404)


def _editable(request: Request, conn: sqlite3.Connection, code: str) -> sqlite3.Row | Response:
    row = _resolve(request, conn, code)
    if isinstance(row, sqlite3.Row) and row["is_system"]:
        return _message(
            request, "Not allowed", "The Inbox cannot be renamed, moved or deleted.", 400
        )
    return row


INT_MAX = 2**63 - 1  # SQLite's largest integer


def _opt_int(params: Any, key: str) -> int | None:
    value = (params.get(key) or "").strip()
    if not value:
        return None
    try:
        number = int(value)
    except (ValueError, OverflowError) as exc:
        raise HTTPException(status_code=400, detail=f"{key} must be a whole number") from exc
    if number < 0:
        raise HTTPException(status_code=400, detail=f"{key} must not be negative")
    if number > INT_MAX:
        raise HTTPException(status_code=400, detail=f"{key} is too large")
    return number


def _flag(params: Any, key: str) -> bool:
    return (params.get(key) or "") in ("1", "true", "on")


def parse_filters(params: Any) -> search.Filters:
    kind = (params.get("kind") or "").strip() or None
    if kind is not None and kind not in search.KINDS:
        raise HTTPException(status_code=400, detail="kind must be consumable or belonging")
    return search.Filters(
        loc=_opt_int(params, "loc"),
        sub=_flag(params, "sub"),
        category=_opt_int(params, "category"),
        kind=kind,
        review=_flag(params, "review"),
        expiring_days=_opt_int(params, "expiring_days"),
    )


def _today(settings: Settings):
    return datetime.now(ZoneInfo(settings.tz)).date()


# ---------------------------------------------------------------- home and search


@router.get("/")
def home(request: Request, conn: Conn) -> Response:
    review_count = conn.execute(
        "SELECT count(*) FROM items WHERE needs_review = 1 AND deleted_at IS NULL"
    ).fetchone()[0]
    return templates.TemplateResponse(
        request,
        "home.html",
        {"tree": locations.tree(conn), "review_count": review_count},
    )


@router.get("/search")
def search_page(request: Request, conn: Conn, q: str = "") -> Response:
    filters = parse_filters(request.query_params)
    results = (
        search.search(conn, q, filters, today=_today(_settings(request)))
        if q.strip() or filters.active
        else []
    )
    context = {
        "q": q,
        "filters": filters,
        "results": results,
        "searched": bool(q.strip() or filters.active),
    }
    if is_htmx(request):
        return templates.TemplateResponse(request, "partials/search_results.html", context)
    context.update(locations=locations.all_with_paths(conn), categories=categories.list_all(conn))
    return templates.TemplateResponse(request, "search.html", context)


# ---------------------------------------------------------------- location pages


def _apply_pending_move(
    request: Request, conn: sqlite3.Connection, location_id: int
) -> list[tuple[sqlite3.Row, int]]:
    """Scan-to-move: move the session's unexpired pending items here (clearing it).
    Returns [(moved item, previous location id)] for the undo toasts."""
    token_hash = request.state.session.token_hash
    pending = sessions.take_pending_move(conn, token_hash, sessions.now(request))
    if pending is None:
        return []
    try:
        previous = items.move_items(conn, pending.item_ids, location_id)
    except items.ItemError:
        return []
    return [(items.get(conn, item_id), prev) for item_id, prev in previous.items()]


@router.get("/l/{code}")
def location_page(request: Request, conn: Conn, code: str, sub: str = "") -> Response:
    row = _resolve(request, conn, code)
    if isinstance(row, Response):
        # A removed location's tag leaves a pending move waiting for another scan.
        if row.status_code == 410 and sessions.get_pending_move(
            conn, request.state.session.token_hash, sessions.now(request)
        ):
            return _resolve(request, conn, code, pending_move=True)
        return row
    moved = _apply_pending_move(request, conn, row["id"])
    subtree = sub in ("1", "true", "on")
    location_ids = locations.subtree_ids(conn, row["id"]) if subtree else [row["id"]]
    children = locations.children(conn, row["id"])
    return templates.TemplateResponse(
        request,
        "location.html",
        {
            "location": row,
            "breadcrumb": locations.ancestors(conn, row["id"]),
            "children": children,
            # Creation order, so "Shelf 1" ... "Shelf 10" print in order.
            "label_children": sorted(row["id"] for row in children)[:IDS_MAX],
            "items": items.list_in(conn, location_ids),
            "subtree": subtree,
            "url": permanent_url(_settings(request), row["short_code"]),
            "moved": moved,
        },
    )


@router.get("/l/{code}/qr.svg")
def location_qr_svg(request: Request, conn: Conn, code: str) -> Response:
    row = _resolve(request, conn, code)
    if isinstance(row, Response):
        return row
    return Response(
        qr.svg(permanent_url(_settings(request), row["short_code"])),
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'inline; filename="location-{row["short_code"]}.svg"'},
    )


@router.get("/l/{code}/qr.png")
def location_qr_png(
    request: Request,
    conn: Conn,
    code: str,
    scale: Annotated[int, Query(ge=qr.PNG_SCALE_MIN, le=qr.PNG_SCALE_MAX)] = 10,
) -> Response:
    row = _resolve(request, conn, code)
    if isinstance(row, Response):
        return row
    return Response(
        qr.png(permanent_url(_settings(request), row["short_code"]), scale=scale),
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="location-{row["short_code"]}.png"'},
    )


@router.get("/l/{code}/label")
def location_label(request: Request, conn: Conn, code: str) -> Response:
    row = _resolve(request, conn, code)
    if isinstance(row, Response):
        return row
    url = permanent_url(_settings(request), row["short_code"])
    return templates.TemplateResponse(
        request,
        "label.html",
        {
            "location": row,
            "path": locations.path(conn, row["id"]),
            "url": url,
            "qr_svg": qr.svg_inline(url),
        },
    )


# ---------------------------------------------------------------- location forms


@router.post("/locations")
def location_create(request: Request, conn: Conn, form: Form) -> Response:
    parent_id = _opt_int(form, "parent_id")
    try:
        _, code = locations.create(conn, form.get("name", ""), parent_id)
    except locations.LocationError as exc:
        return _message(request, "Could not add location", str(exc), 400)
    return _redirect(f"/l/{code}")


@router.get("/l/{code}/edit")
def location_edit_form(request: Request, conn: Conn, code: str) -> Response:
    row = _editable(request, conn, code)
    if isinstance(row, Response):
        return row
    return _location_form(request, conn, row, None, 200)


def _location_form(
    request: Request, conn: sqlite3.Connection, row: sqlite3.Row, error: str | None, status: int
) -> Response:
    excluded = set(locations.subtree_ids(conn, row["id"]))
    parents = [loc for loc in locations.all_with_paths(conn) if loc["id"] not in excluded]
    return templates.TemplateResponse(
        request,
        "location_form.html",
        {"location": row, "parents": parents, "error": error},
        status_code=status,
    )


@router.post("/l/{code}/rename")
def location_rename(request: Request, conn: Conn, code: str, form: Form) -> Response:
    row = _editable(request, conn, code)
    if isinstance(row, Response):
        return row
    try:
        locations.rename(conn, row["id"], form.get("name", ""))
    except locations.LocationError as exc:
        return _location_form(request, conn, row, str(exc), 400)
    return _redirect(f"/l/{row['short_code']}")


@router.post("/l/{code}/move")
def location_move(request: Request, conn: Conn, code: str, form: Form) -> Response:
    row = _editable(request, conn, code)
    if isinstance(row, Response):
        return row
    try:
        locations.move(conn, row["id"], _opt_int(form, "parent_id"))
    except locations.LocationError as exc:
        return _location_form(request, conn, row, str(exc), 400)
    return _redirect(f"/l/{row['short_code']}")


@router.get("/l/{code}/delete")
def location_delete_confirm(request: Request, conn: Conn, code: str) -> Response:
    row = _editable(request, conn, code)
    if isinstance(row, Response):
        return row
    parent = locations.get(conn, row["parent_id"]) if row["parent_id"] else None
    return templates.TemplateResponse(
        request,
        "location_delete.html",
        {
            "location": row,
            "counts": locations.delete_counts(conn, row["id"]),
            "parent": parent,
            "inbox": locations.inbox(conn),
        },
    )


@router.post("/l/{code}/delete")
def location_delete(request: Request, conn: Conn, code: str) -> Response:
    row = _editable(request, conn, code)
    if isinstance(row, Response):
        return row
    locations.delete(conn, row["id"])
    parent = locations.get(conn, row["parent_id"]) if row["parent_id"] else None
    return _redirect(f"/l/{parent['short_code']}" if parent else "/")


# ---------------------------------------------------------------- bulk add and label sheets

IDS_MAX = 100
ID_DIGITS_MAX = 18  # fits SQLite's integer
NAMES_CHARS_MAX = 20_000


def _id_list(params: Any, key: str) -> list[int]:
    """Comma-separated ids, deduplicated in order; at most IDS_MAX."""
    parts = [part.strip() for part in (params.get(key) or "").split(",") if part.strip()]
    if not all(part.isascii() and part.isdigit() and len(part) <= ID_DIGITS_MAX for part in parts):
        raise HTTPException(status_code=400, detail=f"{key} must be whole numbers, comma-separated")
    if len(parts) > IDS_MAX:
        raise HTTPException(status_code=400, detail=f"at most {IDS_MAX} {key}")
    return list(dict.fromkeys(int(part) for part in parts))


def _live_rows(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    """The live locations among ``ids``, in order; unknown and deleted ones are left out."""
    return [row for row in (locations.get(conn, i) for i in ids) if row is not None]


def _bulk_form(
    request: Request,
    conn: sqlite3.Connection,
    parent: sqlite3.Row | None,
    names: str = "",
    error: str | None = None,
    status: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "location_bulk.html",
        {
            "parent": parent,
            "path": locations.path(conn, parent["id"]) if parent else "",
            "names": names,
            "error": error,
            "max": locations.BULK_MAX,
        },
        status_code=status,
    )


@router.get("/locations/bulk")
def location_bulk_form(request: Request, conn: Conn) -> Response:
    parent_id = _opt_int(request.query_params, "parent")
    parent = locations.get(conn, parent_id) if parent_id is not None else None
    if parent_id is not None and parent is None:
        return _message(request, "Not found", "This location does not exist or was deleted.", 404)
    return _bulk_form(request, conn, parent)


@router.post("/locations/bulk")
def location_bulk_create(request: Request, conn: Conn, form: Form) -> Response:
    """Add one location per line under ``parent_id``; then show what was created (PRG)."""
    parent_id = _opt_int(form, "parent_id")
    parent = locations.get(conn, parent_id) if parent_id is not None else None
    if parent_id is not None and parent is None:
        return _message(request, "Could not add locations", "Location not found.", 400)
    names = form.get("names", "")
    if len(names) > NAMES_CHARS_MAX:
        message = f"The list is too long (over {NAMES_CHARS_MAX:,} characters). Nothing was added."
        return _bulk_form(request, conn, parent, "", message, 400)
    try:
        result = locations.create_many(conn, names.splitlines(), parent_id)
    except locations.LocationError as exc:
        return _bulk_form(request, conn, parent, names, str(exc), 400)
    query = {
        "parent": "" if parent_id is None else str(parent_id),
        "ids": ",".join(str(i) for i in result.created),
        "skipped": ",".join(str(i) for _, i in result.skipped),
    }
    return _redirect(f"/locations/bulk/done?{urlencode(query)}")


@router.get("/locations/bulk/done")
def location_bulk_done(request: Request, conn: Conn) -> Response:
    params = request.query_params
    parent_id = _opt_int(params, "parent")
    created_ids = _id_list(params, "ids")
    created = _live_rows(conn, created_ids)
    skipped = [
        (row, "listed more than once" if row["id"] in created_ids else "already exists here")
        for row in _live_rows(conn, _id_list(params, "skipped"))
    ]
    return templates.TemplateResponse(
        request,
        "location_bulk_done.html",
        {
            "parent": locations.get(conn, parent_id) if parent_id is not None else None,
            "created": created,
            "skipped": skipped,
            "label_ids": ",".join(str(row["id"]) for row in created),
        },
    )


def _start(params: Any, key: str, high: int) -> int:
    value = _opt_int(params, key)
    if value is None:
        return 1
    if not 1 <= value <= high:
        raise HTTPException(status_code=400, detail=f"{key} must be 1 to {high}")
    return value


def _offset(params: Any, key: str) -> float:
    raw = (params.get(key) or "").strip()
    try:
        value = float(raw or 0)
    except ValueError:
        value = float("nan")
    if not label_sheet.valid_offset(value):
        raise HTTPException(status_code=400, detail=f"{key} must be -5 to 5 in steps of 0.25")
    return value + 0.0  # no "-0"


def _offset_words(dx: float, dy: float) -> str:
    parts = [
        f"{abs(value):g} mm {words[value < 0]}"
        for value, words in ((dx, ("right", "left")), (dy, ("down", "up")))
        if value
    ]
    return ", ".join(parts) or "none"


@router.get("/labels")
def label_sheet_page(request: Request, conn: Conn) -> Response:
    """Printable 21-up A4 sticker sheets for ``ids``, starting at (start_row, start_col) of the
    first sheet and shifted by the printer calibration (dx, dy mm). ``test=1`` prints only the
    label outlines, to check alignment."""
    params = request.query_params
    ids = _id_list(params, "ids")
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    start_row = _start(params, "start_row", label_sheet.ROWS)
    start_col = _start(params, "start_col", label_sheet.COLS)
    dx, dy = _offset(params, "dx"), _offset(params, "dy")
    test = _flag(params, "test")
    rows = _live_rows(conn, ids)
    if not rows:
        return _message(request, "Nothing to print", "None of these locations exist any more.", 404)
    labels: list[dict | None] = []
    if test:
        cells = label_sheet.layout(label_sheet.PER_SHEET, 1, 1, dx, dy)
        labels = [None] * len(cells)
    else:
        for row in rows:
            url = permanent_url(_settings(request), row["short_code"])
            parents = locations.ancestors(conn, row["id"])[:-1]
            labels.append(
                {
                    "name": row["name"],
                    "code": row["short_code"],
                    "url": url,
                    "url_text": url.split("://", 1)[-1],
                    "path": search.PATH_SEP.join(parent["name"] for parent in parents),
                    "qr_svg": qr.svg_inline(url, scale=1, omitsize=True),
                }
            )
        cells = label_sheet.layout(len(labels), start_row, start_col, dx, dy)
    sheets: list[list[tuple[label_sheet.Cell, dict | None]]] = []
    for cell, label in zip(cells, labels, strict=True):
        if cell.sheet == len(sheets):
            sheets.append([])
        sheets[-1].append((cell, label))
    return templates.TemplateResponse(
        request,
        "labels.html",
        {
            "sheets": sheets,
            "test": test,
            "count": len(rows),
            "start_row": start_row,
            "start_col": start_col,
            "first": (start_row - 1) * label_sheet.COLS + start_col - 1,
            "rows": label_sheet.ROWS,
            "cols": label_sheet.COLS,
            "dx": f"{dx:g}",
            "dy": f"{dy:g}",
            "offset_words": _offset_words(dx, dy),
            "ids": ",".join(str(row["id"]) for row in rows),
        },
    )


# ---------------------------------------------------------------- item form


def _item_form(
    request: Request,
    conn: sqlite3.Connection,
    values: dict[str, Any],
    errors: dict[str, str],
    item_id: int | None,
    status: int = 200,
    from_review: bool = False,
) -> Response:
    return templates.TemplateResponse(
        request,
        "item_form.html",
        {
            "values": values,
            "errors": errors,
            "item_id": item_id,
            "from_review": from_review,
            "locations": locations.all_with_paths(conn),
            "categories": categories.list_all(conn),
            "kinds": search.KINDS,
            # "Where it was seen" (spec-multiphoto R15): photos with this item's boxes.
            "sightings": boxes.sightings(conn, item_id) if item_id is not None else [],
        },
        status_code=status,
    )


def _back_to(conn: sqlite3.Connection, location_id: int) -> Response:
    row = locations.get(conn, location_id)
    return _redirect(f"/l/{row['short_code']}" if row else "/")


@router.get("/items/new")
def item_new_form(request: Request, conn: Conn, loc: int | None = None) -> Response:
    location_id = loc
    if location_id is None or locations.get(conn, location_id) is None:
        location_id = locations.inbox(conn)["id"]
    values = {"quantity": "1", "kind": "belonging", "location_id": str(location_id)}
    return _item_form(request, conn, values, {}, None)


@router.post("/items/new")
def item_create(request: Request, conn: Conn, form: Form) -> Response:
    data, errors = items.parse_form(form)
    if data is not None:
        try:
            items.create(conn, data)
        except items.ItemError as exc:
            errors = {"location_id": str(exc)}
        else:
            return _back_to(conn, data.location_id)
    return _item_form(request, conn, form, errors, None, 400)


@router.get("/items/{item_id}/edit")
def item_edit_form(
    request: Request,
    conn: Conn,
    item_id: int,
    from_: Annotated[str, Query(alias="from")] = "",
) -> Response:
    """The edit form. ``?from=review`` (the review queue's Edit) makes saving also clear
    needs_review and return to /review."""
    row = items.get(conn, item_id)
    if row is None:
        return _message(request, "Not found", "This item does not exist or was used up.", 404)
    values = {
        "name": row["name"],
        "quantity": str(row["quantity"]),
        "kind": row["kind"],
        "category": row["category_name"] or "",
        "location_id": str(row["location_id"]),
        "expiry_date": row["expiry_date"] or "",
        "purchase_date": row["purchase_date"] or "",
        "price": items.format_price(row["price_cents"]),
        "notes": row["notes"] or "",
    }
    return _item_form(request, conn, values, {}, item_id, from_review=from_ == "review")


@router.post("/items/{item_id}/edit")
def item_update(request: Request, conn: Conn, item_id: int, form: Form) -> Response:
    if items.get(conn, item_id) is None:
        return _message(request, "Not found", "This item does not exist or was used up.", 404)
    from_review = form.get("from") == "review"
    data, errors = items.parse_form(form)
    if data is not None:
        try:
            items.update(conn, item_id, data, clear_review=from_review)
        except items.ItemError as exc:
            errors = {"location_id": str(exc)}
        else:
            return _redirect("/review") if from_review else _back_to(conn, data.location_id)
    return _item_form(request, conn, form, errors, item_id, 400, from_review=from_review)


# ---------------------------------------------------------------- htmx quick actions


def _live_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row:
    row = items.get(conn, item_id)
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    return row


def _row(request: Request, conn: sqlite3.Connection, item_id: int) -> Response:
    item = _live_item(conn, item_id)
    return templates.TemplateResponse(request, "partials/item_row.html", {"item": item})


@router.post("/items/{item_id}/quantity", dependencies=HTMX)
def item_adjust(request: Request, conn: Conn, item_id: int, form: Form) -> Response:
    if form.get("delta") not in ("1", "-1", "+1"):
        raise HTTPException(status_code=400, detail="delta must be 1 or -1")
    if items.adjust_quantity(conn, item_id, int(form["delta"])) is None:
        raise HTTPException(status_code=404, detail="item not found")
    return _row(request, conn, item_id)


@router.post("/items/{item_id}/used-up", dependencies=HTMX)
def item_used_up(request: Request, conn: Conn, item_id: int) -> Response:
    if not items.use_up(conn, item_id):
        raise HTTPException(status_code=404, detail="item not found")
    row = items.get(conn, item_id, include_deleted=True)
    return templates.TemplateResponse(request, "partials/item_used_up.html", {"item": row})


@router.post("/items/{item_id}/restore", dependencies=HTMX)
def item_restore(request: Request, conn: Conn, item_id: int) -> Response:
    if not items.undo_use_up(conn, item_id):
        raise HTTPException(status_code=404, detail="item is not used up")
    return _row(request, conn, item_id)


@router.post("/items/{item_id}/move", dependencies=HTMX)
def item_move(request: Request, conn: Conn, item_id: int, form: Form) -> Response:
    """Move one item. The row becomes a "Moved to ..." toast with Undo; the Undo posts back
    here with ``undo=1`` and the previous location, and gets the normal row back."""
    location_id = _opt_int(form, "location_id")
    if location_id is None:
        raise HTTPException(status_code=400, detail="location_id is required")
    _live_item(conn, item_id)
    try:
        previous = items.move_item(conn, item_id, location_id)
    except items.ItemError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if previous is None or _flag(form, "undo"):
        return _row(request, conn, item_id)
    return templates.TemplateResponse(
        request,
        "partials/item_moved.html",
        {"item": items.get(conn, item_id), "previous_id": previous},
    )


@router.get("/items/{item_id}/actions")
def item_actions(request: Request, conn: Conn, item_id: int) -> Response:
    """The ⋯ action sheet (read-only): Used up, Move to (recent, parent, siblings, search),
    Scan destination tag, Edit."""
    item = _live_item(conn, item_id)
    return templates.TemplateResponse(
        request,
        "partials/action_sheet.html",
        {"item": item, "targets": locations.move_targets(conn, item["location_id"])},
    )


@router.get("/items/{item_id}/move-targets")
def item_move_search(request: Request, conn: Conn, item_id: int, q: str = "") -> Response:
    """Action-sheet location search: tap-to-move buttons for matching locations."""
    item = _live_item(conn, item_id)
    targets = [loc for loc in locations.find_by_path(conn, q) if loc["id"] != item["location_id"]]
    return templates.TemplateResponse(
        request,
        "partials/move_targets.html",
        {"item": item, "targets": targets, "searched": bool(q.strip())},
    )


@router.post("/items/{item_id}/pending-move", dependencies=HTMX)
def item_scan_move(request: Request, conn: Conn, item_id: int) -> Response:
    """Scan destination tag: the next scanned location page (within 10 minutes) receives
    the item. Replaces the sheet with a hint and a Cancel button."""
    item = _live_item(conn, item_id)
    sessions.set_pending_move(
        conn, request.state.session.token_hash, [item_id], sessions.now(request)
    )
    return templates.TemplateResponse(request, "partials/scan_hint.html", {"item": item})


@router.post("/pending-move/cancel", dependencies=HTMX)
def pending_move_cancel(request: Request, conn: Conn) -> Response:
    """Cancel scan-to-move; the empty response closes the sheet."""
    sessions.clear_pending_move(conn, request.state.session.token_hash)
    return Response("", media_type="text/html")

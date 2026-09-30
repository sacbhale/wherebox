"""Capture endpoints (plan 3.6 step 1): the capture panel, uploads and text, status polling,
retry and the captures list. Mutations need HX-Request (plus the global origin check)."""

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.auth.csrf import require_htmx
from app.auth.sessions import now as request_now
from app.capture import service
from app.config import Settings
from app.db.deps import Conn
from app.db.timestamps import utc_iso
from app.inventory import locations
from app.templating import templates

router = APIRouter()

HTMX = [Depends(require_htmx)]
# htmx stops polling when it receives this status (and still swaps the content).
STOP_POLLING = 286
MAX_TEXT_CHARS = 2000

Upload = Annotated[UploadFile | None, File()]
FormStr = Annotated[str, Form()]


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _message(request: Request, message: str, status: int) -> Response:
    return templates.TemplateResponse(
        request, "capture_message.html", {"message": message}, status_code=status
    )


def _status(request: Request, conn: sqlite3.Connection, capture_id: int) -> Response:
    capture = service.get(conn, capture_id)
    if capture is None:
        raise HTTPException(status_code=404, detail="capture not found")
    finished = capture["status"] in ("done", "failed")
    return templates.TemplateResponse(
        request,
        "partials/capture_status.html",
        {
            "capture": capture,
            "position": 0 if finished else service.queue_position(conn, capture),
            "outcome": service.outcome(conn, capture_id) if finished else [],
            "now": utc_iso(request_now(request)),
        },
        status_code=STOP_POLLING if finished else 200,
    )


def wake_worker(request: Request) -> None:
    """Start the new work now instead of after the worker's idle wait (when it runs)."""
    worker = getattr(request.app.state, "worker", None)
    if worker is not None:
        worker.wake()


def _location_id(conn: sqlite3.Connection, raw: str, required: bool) -> int | None:
    raw = raw.strip()
    if not raw:
        if required:
            raise HTTPException(status_code=400, detail="choose a location")
        return None
    try:
        location_id = int(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="location_id must be a number") from exc
    if locations.get(conn, location_id) is None:
        raise HTTPException(status_code=400, detail="that location does not exist")
    return location_id


def _queue(
    request: Request,
    conn: sqlite3.Connection,
    capture_type: str,
    *,
    location_id: int | None = None,
    upload: UploadFile | None = None,
    text: str | None = None,
) -> Response:
    """Shared tail of the capture POSTs: gate, store the upload, queue, wake the worker."""
    settings = _settings(request)
    moment = request_now(request)
    blocked = service.capture_blocked_reason(conn, settings, moment)
    if blocked:
        return _message(request, blocked, 409)
    saved = None
    if upload is not None:
        try:
            saved = service.save_upload(settings, upload.file, upload.filename, moment)
        except service.UploadError as exc:
            return _message(request, str(exc), exc.status)
    try:
        capture_id = service.create_capture(
            conn,
            capture_type=capture_type,
            now=moment,
            location_id=location_id,
            input_text=text,
            upload=saved,
        )
    except BaseException:
        if saved is not None:
            service.discard_upload(settings, saved)
        raise
    wake_worker(request)
    return _status(request, conn, capture_id)


def _panel(
    request: Request,
    conn: sqlite3.Connection,
    location: sqlite3.Row | None,
    template: str,
    reconcile: bool = False,
) -> Response:
    recent = locations.recent(conn, 1)
    default_id = recent[0]["id"] if recent else locations.inbox(conn)["id"]
    moment = request_now(request)
    active = service.active(conn, location["id"] if location else None)
    return templates.TemplateResponse(
        request,
        template,
        {
            "location": location,
            "locations": [] if location else locations.all_with_paths(conn),
            "default_id": default_id,
            "blocked": service.capture_blocked_reason(conn, _settings(request), moment),
            "active": active,
            "positions": {row["id"]: service.queue_position(conn, row) for row in active},
            "now": utc_iso(moment),
            "reconcile": reconcile,
        },
    )


@router.get("/captures/panel")
def capture_panel(request: Request, conn: Conn, loc: int | None = None) -> Response:
    """The capture forms (loaded into the location page and home): for one location (photo,
    text) or for home (photo with a location picker defaulting to the last used location,
    receipt, text). Buttons are disabled while AI is off or the budget is reached (AC-24)."""
    location = locations.get(conn, loc) if loc is not None else None
    if loc is not None and location is None:
        raise HTTPException(status_code=404, detail="location not found")
    return _panel(request, conn, location, "capture_panel.html")


@router.get("/l/{code}/capture")
def capture_page(request: Request, conn: Conn, code: str) -> Response:
    """The location's capture forms as a page of their own (works without JavaScript)."""
    return _location_capture_page(request, conn, code, reconcile=False)


@router.get("/l/{code}/reconcile")
def reconcile_page(request: Request, conn: Conn, code: str) -> Response:
    """The same page, headed for reconciling the location against a photo."""
    return _location_capture_page(request, conn, code, reconcile=True)


def _location_capture_page(
    request: Request, conn: sqlite3.Connection, code: str, reconcile: bool
) -> Response:
    status, row = locations.lookup_code(conn, code)
    if status != "live":
        message = (
            "This location was removed." if status == "deleted" else "No location has this code."
        )
        return templates.TemplateResponse(
            request,
            "message.html",
            {"title": "Not found", "message": message},
            status_code=410 if status == "deleted" else 404,
        )
    return _panel(request, conn, row, "capture_page.html", reconcile=reconcile)


@router.post("/captures/photo", dependencies=HTMX)
def capture_photo(
    request: Request, conn: Conn, photo: Upload = None, location_id: FormStr = ""
) -> Response:
    target = _location_id(conn, location_id, required=True)
    if photo is None:
        return _message(request, "Choose a photo to upload.", 400)
    return _queue(request, conn, "photo", location_id=target, upload=photo)


@router.post("/captures/reconcile", dependencies=HTMX)
def capture_reconcile(
    request: Request, conn: Conn, photo: Upload = None, location_id: FormStr = ""
) -> Response:
    """Reconcile a location against a photo: new items are added, differences become review
    proposals (AC-20)."""
    target = _location_id(conn, location_id, required=True)
    if photo is None:
        return _message(request, "Choose a photo of the whole location.", 400)
    return _queue(request, conn, "reconcile", location_id=target, upload=photo)


@router.post("/captures/receipt", dependencies=HTMX)
def capture_receipt(request: Request, conn: Conn, receipt: Upload = None) -> Response:
    if receipt is None:
        return _message(request, "Choose a receipt photo or PDF to upload.", 400)
    return _queue(request, conn, "receipt", upload=receipt)


@router.post("/captures/text", dependencies=HTMX)
def capture_text(
    request: Request, conn: Conn, text: FormStr = "", location_id: FormStr = ""
) -> Response:
    target = _location_id(conn, location_id, required=False)
    cleaned = text.strip()
    if not cleaned:
        return _message(request, "Type or say what you want to add.", 400)
    if len(cleaned) > MAX_TEXT_CHARS:
        return _message(request, f"Keep it under {MAX_TEXT_CHARS} characters.", 400)
    return _queue(request, conn, "text", location_id=target, text=cleaned)


@router.get("/captures/{capture_id}/status")
def capture_status(request: Request, conn: Conn, capture_id: int) -> Response:
    """Polled every 2s by the status partial; 286 (stop polling) once done or failed."""
    return _status(request, conn, capture_id)


@router.post("/captures/{capture_id}/retry", dependencies=HTMX)
def capture_retry(request: Request, conn: Conn, capture_id: int) -> Response:
    if service.get(conn, capture_id) is None:
        raise HTTPException(status_code=404, detail="capture not found")
    if not service.retry(conn, capture_id, request_now(request)):
        raise HTTPException(status_code=409, detail="only failed captures can be retried")
    wake_worker(request)
    return _status(request, conn, capture_id)


@router.get("/captures")
def captures_page(request: Request, conn: Conn) -> Response:
    rows = service.recent(conn)
    return templates.TemplateResponse(
        request,
        "captures.html",
        {
            "captures": rows,
            "outcomes": {
                row["id"]: service.outcome(conn, row["id"])
                for row in rows
                if row["status"] == "done"
            },
            "positions": {
                row["id"]: service.queue_position(conn, row)
                for row in rows
                if row["status"] in ("queued", "processing")
            },
            "now": utc_iso(request_now(request)),
        },
    )

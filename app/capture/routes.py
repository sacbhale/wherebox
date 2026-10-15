"""Capture endpoints (plan 3.6 step 1): the capture panel, uploads and text, status polling,
retry and the captures list. Mutations need HX-Request (plus the global origin check).

File uploads (photo, reconcile, receipt, add another angle) are limited before the body is
parsed: ``upload_size_error`` answers 415 when the body is not multipart/form-data, 413 when
Content-Length exceeds 6 x MAX_UPLOAD_MB + 1 MB and 411 when it is missing (chunked); the
form is then parsed with at most 7 files and 20 fields. Photo and reconcile take 1-6 photos
(``photos``, or the older single ``photo``)."""

import logging
import sqlite3
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth.csrf import require_htmx
from app.auth.sessions import now as request_now
from app.capture import groups, service
from app.config import Settings
from app.db.deps import Conn
from app.db.timestamps import utc_iso
from app.inventory import locations
from app.templating import templates

log = logging.getLogger(__name__)
router = APIRouter()

HTMX = [Depends(require_htmx)]
# htmx stops polling when it receives this status (and still swaps the content).
STOP_POLLING = 286
MAX_TEXT_CHARS = 2000
# Form parsing limits for the upload POSTs: one file more than allowed, so a 7th photo gets
# a clear message instead of a parser error.
MAX_FORM_FILES = groups.MAX_PHOTOS_PER_SUBMISSION + 1
MAX_FORM_FIELDS = 20
TOO_MANY_PHOTOS = f"Send at most {groups.MAX_PHOTOS_PER_SUBMISSION} photos at a time."

FormStr = Annotated[str, Form()]


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _message(request: Request, message: str, status: int) -> Response:
    return templates.TemplateResponse(
        request, "capture_message.html", {"message": message}, status_code=status
    )


VIEWS = ("panel", "list", "group")


def _view(raw: str) -> str:
    return raw if raw in VIEWS else "panel"


def _pin(conn: sqlite3.Connection, group: groups.Group | None) -> dict[str, Any] | None:
    """Where the group page's "Use as location photo" buttons post and the photo pinned now
    ({url, pinned}), or None when they are not offered: a done photo/reconcile group of a
    live location other than the Inbox."""
    if group is None or group.location_id is None:
        return None
    root = group.root
    if root["status"] != "done" or root["type"] not in ("photo", "reconcile"):
        return None
    location = locations.get(conn, group.location_id)
    if location is None or location["is_system"]:
        return None
    return {
        "url": f"/l/{location['short_code']}/overview",
        "pinned": location["overview_photo_id"],
    }


def _status(
    request: Request, conn: sqlite3.Connection, capture_id: int, view: str = "panel"
) -> Response:
    """The status partial in one of VIEWS (see partials/capture_status.html): the panel
    shows a finished photo/reconcile capture's gallery, the group view refreshes the group
    page out of band."""
    capture = service.get(conn, capture_id)
    if capture is None:
        raise HTTPException(status_code=404, detail="capture not found")
    finished = capture["status"] in ("done", "failed")
    done = capture["status"] == "done"
    group = None
    if view == "group" or (view == "panel" and done and capture["type"] in ("photo", "reconcile")):
        group = groups.group(conn, capture_id)
    extension = None
    if done and capture["parent_capture_id"] is not None:
        extension = groups.extension_outcome(conn, capture_id)
    return templates.TemplateResponse(
        request,
        "partials/capture_status.html",
        {
            "capture": capture,
            "position": 0 if finished else service.queue_position(conn, capture),
            "outcome": service.outcome(conn, capture_id) if finished else [],
            "now": utc_iso(request_now(request)),
            "view": view,
            "group": group,
            "extension": extension,
            "pin": _pin(conn, group) if view == "group" else None,
            "oob": True,
        },
        status_code=STOP_POLLING if finished else 200,
    )


def wake_worker(request: Request) -> None:
    """Start the new work now instead of after the worker's idle wait (when it runs)."""
    worker = getattr(request.app.state, "worker", None)
    if worker is not None:
        worker.wake()


def upload_size_error(request: Request) -> Response | None:
    """Dependency of the upload POSTs, resolved before the body is read: a message partial
    with 415 when the body is not multipart/form-data (nothing else is parsed), 411 when
    Content-Length is missing, 413 when it is over 6 x MAX_UPLOAD_MB + 1 MB; None otherwise
    (the endpoint returns the partial when there is one)."""
    settings = _settings(request)
    content_type = request.headers.get("content-type", "")
    if content_type.split(";", 1)[0].strip().lower() != "multipart/form-data":
        log.info("capture upload rejected (415): Content-Type %r", content_type[:100])
        return _message(request, "Send the photos as a file upload.", 415)
    raw = request.headers.get("content-length")
    if raw is None or not raw.strip().isdigit():
        log.info("capture upload rejected (411): no Content-Length")
        return _message(request, "The upload needs a Content-Length; try again.", 411)
    limit_mb = groups.MAX_PHOTOS_PER_SUBMISSION * settings.max_upload_mb + 1
    if int(raw) > limit_mb * 1024 * 1024:
        log.info("capture upload rejected (413): Content-Length %s", raw)
        return _message(
            request,
            f"The upload is larger than {limit_mb} MB; send fewer or smaller photos.",
            413,
        )
    return None


SizeError = Annotated[Response | None, Depends(upload_size_error)]


async def _read_form(request: Request) -> FormData | Response:
    """The multipart form, parsed within MAX_FORM_FILES / MAX_FORM_FIELDS, or a 400 partial."""
    try:
        return await request.form(max_files=MAX_FORM_FILES, max_fields=MAX_FORM_FIELDS)
    except StarletteHTTPException as exc:  # the parser's limits and malformed bodies
        log.info(
            "capture upload rejected (400): %s; Content-Length %s",
            exc.detail,
            request.headers.get("content-length"),
        )
        message = TOO_MANY_PHOTOS if "files" in str(exc.detail) else "The upload could not be read."
        return _message(request, message, 400)


def _files(form: FormData, *names: str) -> list[UploadFile]:
    """Uploaded files of the given fields, in order; empty file inputs (no filename) and
    plain text values are left out."""
    return [
        value
        for name in names
        for value in form.getlist(name)
        if isinstance(value, UploadFile) and value.filename
    ]


def _rejected(request: Request, message: str, status: int, files: list[UploadFile]) -> Response:
    """A 4xx message partial for an upload, logged with the file count and sizes."""
    log.info(
        "capture upload rejected (%s): %s; %d file(s), sizes %s",
        status,
        message,
        len(files),
        [upload.size for upload in files],
    )
    return _message(request, message, status)


def _field(form: FormData, name: str) -> str:
    value = form.get(name)
    return value if isinstance(value, str) else ""


def _location_id(conn: sqlite3.Connection, raw: str, required: bool) -> int | None:
    """The form's location id; HTTPException(400) when missing (if required) or unknown."""
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
    files: list[UploadFile] | None = None,
    text: str | None = None,
    extends: int | None = None,
    view: str = "panel",
) -> Response:
    """Shared tail of the capture POSTs: gate, store the uploads (all or nothing), queue,
    wake the worker."""
    settings = _settings(request)
    files = files or []
    moment = request_now(request)
    blocked = service.capture_blocked_reason(conn, settings, moment)
    if blocked:
        return _rejected(request, blocked, 409, files)
    try:
        saved = service.save_uploads(
            settings,
            [(upload.file, upload.filename) for upload in files],
            moment,
            allow_pdf=capture_type == "receipt",
        )
    except service.UploadError as exc:
        return _rejected(request, str(exc), exc.status, files)
    try:
        capture_id = service.create_capture(
            conn,
            capture_type=capture_type,
            now=moment,
            location_id=location_id,
            input_text=text,
            uploads=saved,
            extends=extends,
        )
    except BaseException as exc:
        for upload in saved:
            service.discard_upload(settings, upload)
        if isinstance(exc, service.UploadError):
            return _rejected(request, str(exc), exc.status, files)
        raise
    wake_worker(request)
    return _status(request, conn, capture_id, view)


def _photo_capture(
    request: Request,
    conn: sqlite3.Connection,
    form: FormData,
    capture_type: str,
    missing: str,
) -> Response:
    """Photo or reconcile: 1-6 photos for a location."""
    files = _files(form, "photos", "photo")
    try:
        target = _location_id(conn, _field(form, "location_id"), required=True)
    except HTTPException as exc:
        return _rejected(request, str(exc.detail).capitalize() + ".", 400, files)
    if not files:
        return _rejected(request, missing, 400, files)
    if len(files) > groups.MAX_PHOTOS_PER_SUBMISSION:
        return _rejected(request, TOO_MANY_PHOTOS, 400, files)
    return _queue(request, conn, capture_type, location_id=target, files=files)


async def _upload_endpoint(
    request: Request,
    size_error: Response | None,
    handle: Callable[[Request, FormData], Response],
) -> Response:
    """Parse the form (after the size check), run ``handle(request, form)`` on the
    threadpool (it touches the database and the disk), close the form's files."""
    if size_error is not None:
        return size_error
    form = await _read_form(request)
    if isinstance(form, Response):
        return form
    try:
        return await run_in_threadpool(handle, request, form)
    finally:
        await form.close()


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
async def capture_photo(request: Request, conn: Conn, size_error: SizeError) -> Response:
    """A photo capture of a location: 1-6 photos (``photos``; the single ``photo`` field is
    still accepted), stored before any AI call and sent in one call."""

    def handle(request: Request, form: FormData) -> Response:
        return _photo_capture(request, conn, form, "photo", "Choose a photo to upload.")

    return await _upload_endpoint(request, size_error, handle)


@router.post("/captures/reconcile", dependencies=HTMX)
async def capture_reconcile(request: Request, conn: Conn, size_error: SizeError) -> Response:
    """Reconcile a location against 1-6 photos: new items are added, differences become
    review proposals (AC-20)."""

    def handle(request: Request, form: FormData) -> Response:
        return _photo_capture(
            request, conn, form, "reconcile", "Choose a photo of the whole location."
        )

    return await _upload_endpoint(request, size_error, handle)


@router.post("/captures/receipt", dependencies=HTMX)
async def capture_receipt(request: Request, conn: Conn, size_error: SizeError) -> Response:
    """A receipt: one photo or PDF."""

    def handle(request: Request, form: FormData) -> Response:
        files = _files(form, "receipt")
        if not files:
            return _rejected(request, "Choose a receipt photo or PDF to upload.", 400, files)
        if len(files) > 1:
            return _rejected(request, "Send one receipt at a time.", 400, files)
        return _queue(request, conn, "receipt", files=files)

    return await _upload_endpoint(request, size_error, handle)


@router.post("/captures/{capture_id}/extend", dependencies=HTMX)
async def capture_extend(
    request: Request, conn: Conn, capture_id: int, size_error: SizeError
) -> Response:
    """ "Add another angle" to a done photo capture (root or extension): 1-6 more photos
    (at most 12 per group) as an extension capture of the group's root, at its location.
    Same gates as any capture; answers with the new capture's status partial."""

    def handle(request: Request, form: FormData) -> Response:
        files = _files(form, "photos", "photo")
        target = service.get(conn, capture_id)
        refusal = groups.extend_refusal(target)
        if refusal:
            return _rejected(request, refusal, 404 if target is None else 409, files)
        if not files:
            return _rejected(request, "Choose a photo to upload.", 400, files)
        if len(files) > groups.MAX_PHOTOS_PER_SUBMISSION:
            return _rejected(request, TOO_MANY_PHOTOS, 400, files)
        return _queue(request, conn, "photo", files=files, extends=capture_id, view="group")

    return await _upload_endpoint(request, size_error, handle)


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


ViewParam = Annotated[str, Query(alias="in")]


@router.get("/captures/{capture_id}/status")
def capture_status(request: Request, conn: Conn, capture_id: int, in_: ViewParam = "") -> Response:
    """Polled every 2s by the status partial; 286 (stop polling) once done or failed."""
    return _status(request, conn, capture_id, _view(in_))


@router.post("/captures/{capture_id}/retry", dependencies=HTMX)
def capture_retry(request: Request, conn: Conn, capture_id: int, in_: ViewParam = "") -> Response:
    """Re-queue a failed capture; a failed extension that no longer fits its group (full,
    or its photos were added since) gets a 409 message partial (shown by the Retry button's
    data-show-errors)."""
    if service.get(conn, capture_id) is None:
        raise HTTPException(status_code=404, detail="capture not found")
    try:
        retried = service.retry(conn, capture_id, request_now(request))
    except service.RetryRefused as exc:
        return _message(request, str(exc), 409)
    if not retried:
        raise HTTPException(status_code=409, detail="only failed captures can be retried")
    wake_worker(request)
    return _status(request, conn, capture_id, _view(in_))


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
            "extensions": {
                row["id"]: groups.extension_outcome(conn, row["id"])
                for row in rows
                if row["status"] == "done" and row["parent_capture_id"] is not None
            },
            "now": utc_iso(request_now(request)),
        },
    )


@router.get("/captures/{capture_id}")
def capture_detail(
    request: Request, conn: Conn, capture_id: int, item: int | None = None
) -> Response:
    """A capture group's page (root or extension id -> the root's group): the annotated
    gallery of all its photos and items, "Add another angle" (photo captures, within the
    group's photo capacity) and "Use for model comparison". ``?item=<id>`` starts with
    that item highlighted. Declared after /captures/panel so that path is not taken for
    an id."""
    group = groups.group(conn, capture_id)
    if group is None:
        return templates.TemplateResponse(
            request,
            "message.html",
            {"title": "Not found", "message": "This capture does not exist."},
            status_code=404,
        )
    root = service.get(conn, group.root_id)
    return templates.TemplateResponse(
        request,
        "capture_detail.html",
        {
            "group": group,
            "root": root,
            "highlight": item,
            "refusal": groups.extend_refusal(root),
            "pin": _pin(conn, group),
            "now": utc_iso(request_now(request)),
        },
    )

"""The review queue page (AC-7) and capture photos.

Every review action is one htmx POST that answers with an empty body, which the row's
``hx-swap="outerHTML"`` turns into "the row is gone". Writes go through app.inventory.review,
app.inventory.items and app.capture.service.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from app.auth.csrf import require_htmx
from app.auth.sessions import now as request_now
from app.capture import boxes, service
from app.capture.routes import wake_worker
from app.db.deps import Conn
from app.inventory import items, review
from app.templating import templates

router = APIRouter()

HTMX = [Depends(require_htmx)]


def _gone() -> Response:
    return Response("", media_type="text/html")


def _require(done: bool, detail: str) -> Response:
    if not done:
        raise HTTPException(status_code=404, detail=detail)
    return _gone()


@router.get("/review")
def review_page(request: Request, conn: Conn) -> Response:
    flagged = review.flagged_items(conn)
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "failed": review.failed_captures(conn),
            "flagged": flagged,
            # Flagged items with a box show a crop of it (spec-multiphoto Part 5).
            "crops": boxes.review_crops(conn, [row["id"] for row in flagged]),
            "missing": review.open_proposals(conn, "missing"),
            "quantity": review.open_proposals(conn, "quantity"),
            "thumbnail_suffixes": boxes.THUMBNAIL_SUFFIXES,
        },
    )


@router.post("/review/captures/{capture_id}/retry", dependencies=HTMX)
def review_retry(request: Request, conn: Conn, capture_id: int) -> Response:
    """Re-queue a failed capture (the row goes away). A failed extension that no longer fits
    its group answers 409 with the message partial, shown in the row (data-show-errors)."""
    try:
        done = service.retry(conn, capture_id, request_now(request))
    except service.RetryRefused as exc:
        return templates.TemplateResponse(
            request, "capture_message.html", {"message": str(exc)}, status_code=409
        )
    if done:
        wake_worker(request)
    return _require(done, "no failed capture to retry")


@router.post("/review/items/{item_id}/keep", dependencies=HTMX)
def review_keep(conn: Conn, item_id: int) -> Response:
    return _require(review.keep_item(conn, item_id), "no item to review")


@router.post("/review/items/{item_id}/delete", dependencies=HTMX)
def review_delete(conn: Conn, item_id: int) -> Response:
    return _require(items.use_up(conn, item_id), "item not found")


@router.post("/review/proposals/{proposal_id}/confirm-missing", dependencies=HTMX)
def review_confirm_missing(conn: Conn, proposal_id: int) -> Response:
    return _require(review.confirm_missing(conn, proposal_id), "no open proposal")


@router.post("/review/proposals/{proposal_id}/still-here", dependencies=HTMX)
def review_still_here(conn: Conn, proposal_id: int) -> Response:
    return _require(review.still_here(conn, proposal_id), "no open proposal")


@router.post("/review/proposals/{proposal_id}/accept", dependencies=HTMX)
def review_accept_quantity(conn: Conn, proposal_id: int) -> Response:
    return _require(review.accept_quantity(conn, proposal_id), "no open proposal")


@router.post("/review/proposals/{proposal_id}/dismiss", dependencies=HTMX)
def review_dismiss_quantity(conn: Conn, proposal_id: int) -> Response:
    return _require(review.dismiss_quantity(conn, proposal_id), "no open proposal")


@router.get("/photos/{photo_id}")
def photo_file(request: Request, conn: Conn, photo_id: int) -> Response:
    """A stored capture photo (its display JPEG when there is one). The path comes from the
    database and must resolve inside DATA_DIR/photos, so it can never escape that directory.
    Cached for a day once the display JPEG exists; before that only for a minute, so a page
    opened before the worker prepared the photo does not keep the original for a day."""
    row = review.photo(conn, photo_id)
    if row is None:
        raise HTTPException(status_code=404, detail="photo not found")
    settings = request.app.state.settings
    root = settings.photos_dir.resolve()
    path = (settings.data_dir / (row["display_rel_path"] or row["rel_path"])).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(status_code=404, detail="photo not found")
    max_age = 86400 if row["display_rel_path"] else 60
    return FileResponse(
        path,
        headers={
            "Cache-Control": f"private, max-age={max_age}",
            "X-Content-Type-Options": "nosniff",
        },
    )

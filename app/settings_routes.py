"""GET /settings (read-only; protected by the global auth middleware)."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.db.deps import Conn
from app.settings_info import QR_WARNING, TIMEOUT_NOTE, build_settings_info
from app.templating import templates

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn: Conn) -> HTMLResponse:
    info = build_settings_info(conn, request.app.state.settings)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"info": info, "qr_warning": QR_WARNING, "timeout_note": TIMEOUT_NOTE},
    )

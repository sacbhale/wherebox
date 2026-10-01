"""GET /settings, plus the AI model picker (protected by the global auth middleware; the
POSTs also need HX-Request and pass the global origin check)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.ai import openrouter_catalog
from app.ai.model_choice import model_selectable
from app.ai.registry import ai_disabled_reason
from app.auth.csrf import require_htmx
from app.auth.sessions import now as request_now
from app.capture import model_setting
from app.compare import cases, planning, runs
from app.config import Settings
from app.db.deps import Conn
from app.settings_info import (
    QR_WARNING,
    TIMEOUT_NOTE,
    build_model_section,
    build_settings_info,
    model_results,
)
from app.templating import templates

router = APIRouter()

HTMX = [Depends(require_htmx)]


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _require_selectable(settings: Settings) -> None:
    if not model_selectable(settings):
        raise HTTPException(status_code=404, detail="the model is not selectable")


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, conn: Conn) -> HTMLResponse:
    settings = _settings(request)
    info = build_settings_info(conn, settings, now=request_now(request))
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "info": info,
            "section": build_model_section(conn, settings),
            "compare": {
                "cases": cases.count_live(conn),
                "spend_usd": runs.month_cost_micros(conn, settings, request_now(request)) / 1e6,
                "available": planning.available(settings),
                "unavailable": planning.UNAVAILABLE,
            },
            "qr_warning": QR_WARNING,
            "timeout_note": TIMEOUT_NOTE,
        },
    )


@router.get("/settings/models", response_class=HTMLResponse)
def model_picker_results(
    request: Request,
    conn: Conn,
    q: str = "",
    all_models: Annotated[str, Query(alias="all")] = "",
) -> HTMLResponse:
    """The picker's result list (htmx, searched as the user types). ``all=1`` includes
    models that do not accept images."""
    settings = _settings(request)
    _require_selectable(settings)
    results = model_results(conn, settings, q.strip()[:100], images_only=not all_models)
    return templates.TemplateResponse(request, "partials/model_results.html", {"results": results})


def _section(request: Request, conn: Conn) -> Response:
    """The re-rendered section, plus the page's provider/model line (out of band)."""
    settings = _settings(request)
    section = build_model_section(conn, settings)
    disabled = ai_disabled_reason(settings, section.model) is not None
    return templates.TemplateResponse(
        request,
        "partials/ai_model.html",
        {"section": section, "provider_oob": {"name": settings.ai_provider, "disabled": disabled}},
    )


@router.post("/settings/model", dependencies=HTMX)
def choose_model(request: Request, conn: Conn, model: Annotated[str, Form()]) -> Response:
    """Make ``model`` the active model: the next capture uses it."""
    settings = _settings(request)
    _require_selectable(settings)
    model = model.strip()
    if not openrouter_catalog.valid_model_id(model):
        raise HTTPException(status_code=400, detail="invalid model id")
    catalog = openrouter_catalog.load_catalog(settings)
    if catalog.available:
        info = catalog.get(model)
        if info is None or not info.price_known:
            raise HTTPException(status_code=400, detail="model not in the OpenRouter catalog")
    model_setting.set_model(conn, model)
    return _section(request, conn)


@router.post("/settings/model/reset", dependencies=HTMX)
def reset_model(request: Request, conn: Conn) -> Response:
    """Forget the chosen model: AI_MODEL from .env applies again."""
    _require_selectable(_settings(request))
    model_setting.clear_model(conn)
    return _section(request, conn)

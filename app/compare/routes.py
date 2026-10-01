"""Model comparison pages: cases (made from finished captures), runs, their status and the
report. Writes go through app.compare.cases / runs and app.capture.model_setting; htmx
mutations need HX-Request, plain form POSTs pass the global origin check."""

import dataclasses
import sqlite3
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response

from app.ai import openrouter_catalog
from app.ai.model_choice import model_selectable
from app.auth.csrf import require_htmx
from app.auth.sessions import now as request_now
from app.capture import model_setting, service
from app.capture.budget import budget_micros, month_usage
from app.capture.routes import STOP_POLLING, wake_worker
from app.compare import cases, planning, report, runs
from app.compare.scoring import Expected
from app.config import Settings
from app.db.deps import Conn
from app.inventory.items import format_price
from app.settings_info import model_results
from app.templating import templates

router = APIRouter()
# Capture status rows offer "Use for model comparison" only when comparisons are available.
templates.env.globals["compare_available"] = planning.available

HTMX = [Depends(require_htmx)]
FormList = Annotated[list[str] | None, Form()]
QueryList = Annotated[list[str] | None, Query()]


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _require_available(settings: Settings) -> None:
    if not planning.available(settings):
        raise HTTPException(status_code=404, detail="model comparison is not available")


def _message(request: Request, message: str, status: int) -> Response:
    return templates.TemplateResponse(
        request, "partials/compare_message.html", {"message": message}, status_code=status
    )


def _page_message(request: Request, title: str, message: str, status: int) -> Response:
    return templates.TemplateResponse(
        request, "message.html", {"title": title, "message": message}, status_code=status
    )


def _ints(values: list[str] | None) -> list[int]:
    out = []
    for value in values or []:
        try:
            out.append(int(value))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="case ids must be numbers") from exc
    return out


# ---------------------------------------------------------------- page, picker, selection


def _selection(
    conn: sqlite3.Connection,
    settings: Settings,
    model_ids: list[str],
    image_counts: list[int],
) -> dict[str, object]:
    """The chosen models (kept in catalog order of choice, unknown ids dropped) with the
    rough estimate for the chosen cases (``image_counts``: photos per case)."""
    catalog = openrouter_catalog.load_catalog(settings)
    chosen = []
    for model_id in dict.fromkeys(model_ids):
        info = catalog.get(model_id) if openrouter_catalog.valid_model_id(model_id) else None
        if info is not None and info.price_known and not info.is_router:
            chosen.append(info)
    chosen = chosen[: planning.MAX_MODELS]
    rough = planning.estimate(conn, chosen, image_counts)
    return {
        "chosen": chosen,
        "case_count": len(image_counts),
        "estimate": rough,
        "min_models": planning.MIN_MODELS,
        "max_models": planning.MAX_MODELS,
    }


def _budget(conn: sqlite3.Connection, settings: Settings, request: Request) -> float | None:
    limit = budget_micros(settings)
    if limit is None:
        return None
    spent = month_usage(conn, settings, request_now(request)).cost_micros
    return max(limit - spent, 0) / 1_000_000


def _elapsed(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    return f"{seconds // 60} min {seconds % 60} s" if seconds >= 60 else f"{seconds} s"


def _status_context(
    conn: sqlite3.Connection, settings: Settings, run: sqlite3.Row, now: datetime
) -> dict[str, Any]:
    """What partials/compare_run.html needs: overall and per-model progress, the elapsed
    time and, once finished, the top-ranked model (when any call succeeded)."""
    progress = runs.progress(conn, run["id"])
    end = datetime.fromisoformat(run["finished_at"]) if run["finished_at"] else now
    catalog = openrouter_catalog.load_catalog(settings)
    names = {}
    for model in runs.models_of(run):
        info = catalog.get(model)
        names[model] = info.name if info else model
    top = None
    if run["status"] in runs.FINISHED and progress["ok"]:
        top = report.ranking(conn, run)[0]
    return {
        "run": run,
        "progress": progress,
        "models": runs.model_progress(conn, run, now),
        "names": names,
        "elapsed": _elapsed((end - datetime.fromisoformat(run["created_at"])).total_seconds()),
        "top": top,
    }


@router.get("/compare")
def compare_page(request: Request, conn: Conn) -> Response:
    settings = _settings(request)
    available = planning.available(settings)
    live = cases.live(conn, settings)
    usable = [case for case in live if case.usable]
    default_cases = usable[: planning.MAX_CASES]
    running = runs.running(conn)
    status = _status_context(conn, settings, running, request_now(request)) if running else {}
    # The status's own requests ask for the out-of-band parts only where they have targets.
    status["from_compare"] = bool(available and usable)
    return templates.TemplateResponse(
        request,
        "compare.html",
        status
        | {
            "available": available,
            "unavailable": planning.UNAVAILABLE,
            "cases": live,
            "usable": usable,
            "default_case_ids": {case.id for case in default_cases},
            "max_cases": planning.MAX_CASES,
            "running": running,
            "recent": runs.recent(conn),
            # The catalog is only loaded when comparisons are available.
            "results": model_results(conn, settings, routers=False) if available else None,
            "selection": (
                _selection(conn, settings, [], [case.image_count for case in default_cases])
                if available
                else None
            ),
            "remaining_usd": _budget(conn, settings, request),
        },
    )


@router.get("/compare/models")
def compare_models(
    request: Request,
    conn: Conn,
    q: str = "",
    all_models: Annotated[str, Query(alias="all")] = "",
) -> Response:
    """The comparison picker's result list (htmx search); rows have an "Add" button."""
    settings = _settings(request)
    _require_available(settings)
    results = model_results(conn, settings, q.strip()[:100], not all_models, routers=False)
    return templates.TemplateResponse(request, "partials/compare_models.html", {"results": results})


@router.get("/compare/selection")
def compare_selection(
    request: Request,
    conn: Conn,
    models: QueryList = None,
    cases_: Annotated[list[str] | None, Query(alias="cases")] = None,
    add: str = "",
    remove: str = "",
) -> Response:
    """The chosen models and the rough estimate, re-rendered when a model is added or
    removed or the case selection changes (no state is stored)."""
    settings = _settings(request)
    _require_available(settings)
    chosen = [m for m in (models or []) if m != remove]
    if add:
        chosen.append(add)
    case_ids = sorted(set(_ints(cases_)))
    picked = cases.many(conn, settings, case_ids)
    # A case id that is not (or no longer) there counts as a 1-photo case.
    counts = [picked[i].image_count if i in picked else 1 for i in case_ids]
    selection = _selection(conn, settings, chosen, counts)
    return templates.TemplateResponse(
        request, "partials/compare_selection.html", {"selection": selection}
    )


# ---------------------------------------------------------------- runs


def _run_status(
    request: Request, conn: sqlite3.Connection, run_id: int, *, started: bool = False
) -> Response:
    """The status partial. When the run has just started or is finished, and the request
    comes from /compare's start form (``?from=compare`` on the status's own requests there),
    it also carries the start button (locked / unlocked) and the Past comparisons list out of
    band; the report page has neither, so it never gets them."""
    run = runs.get(conn, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="comparison not found")
    finished = run["status"] in runs.FINISHED
    from_compare = started or request.query_params.get("from") == "compare"
    context = _status_context(conn, _settings(request), run, request_now(request))
    context["from_compare"] = from_compare
    if from_compare and (started or finished):
        context |= {"oob": True, "locked": not finished, "recent": runs.recent(conn)}
    return templates.TemplateResponse(
        request,
        "partials/compare_run.html",
        context,
        status_code=STOP_POLLING if finished else 200,
    )


@router.post("/compare/runs", dependencies=HTMX)
def start_run(
    request: Request,
    conn: Conn,
    models: FormList = None,
    cases_: Annotated[list[str] | None, Form(alias="cases")] = None,
) -> Response:
    """Start a run over the chosen models and cases (409/400 with a message when refused)."""
    settings = _settings(request)
    moment = request_now(request)
    case_ids = _ints(cases_)
    try:
        plan = planning.plan(conn, settings, models or [], case_ids or None, moment)
        run_id = runs.start(
            conn,
            [m.id for m in plan.models],
            [c.id for c in plan.cases],
            plan.estimate.total_micros,
            moment,
        )
    except planning.PlanError as exc:
        return _message(request, str(exc), exc.status)
    except runs.RunConflict as exc:
        return _message(request, str(exc), 409)
    wake_worker(request)
    response = _run_status(request, conn, run_id, started=True)
    response.status_code = 200
    return response


@router.get("/compare/runs/{run_id}/status")
def run_status(request: Request, conn: Conn, run_id: int) -> Response:
    """Polled every 2s by the status partial; 286 (stop polling) once finished."""
    return _run_status(request, conn, run_id)


@router.post("/compare/runs/{run_id}/cancel", dependencies=HTMX)
def cancel_run(request: Request, conn: Conn, run_id: int) -> Response:
    if runs.get(conn, run_id) is None:
        raise HTTPException(status_code=404, detail="comparison not found")
    if not runs.cancel(conn, run_id, request_now(request)):
        raise HTTPException(status_code=409, detail="only a running comparison can be cancelled")
    return _run_status(request, conn, run_id)


@router.get("/compare/runs/{run_id}")
def run_report(request: Request, conn: Conn, run_id: int) -> Response:
    settings = _settings(request)
    data = report.build(conn, settings, run_id)
    if data is None:
        return _page_message(request, "Not found", "This comparison does not exist.", 404)
    return templates.TemplateResponse(
        request,
        "compare_report.html",
        _status_context(conn, settings, data.run, request_now(request)) | {"report": data},
    )


@router.post("/compare/runs/{run_id}/use", dependencies=HTMX)
def use_model(request: Request, conn: Conn, run_id: int, model: Annotated[str, Form()]) -> Response:
    """ "Use this model": make a compared model the active model (when it is selectable)."""
    settings = _settings(request)
    if not model_selectable(settings):
        raise HTTPException(status_code=404, detail="the model is not selectable")
    data = report.build(conn, settings, run_id)
    if data is None:
        raise HTTPException(status_code=404, detail="comparison not found")
    model = model.strip()
    if model not in data.models or model not in data.usable:
        raise HTTPException(status_code=400, detail="that model cannot be used")
    model_setting.set_model(conn, model)
    data = dataclasses.replace(data, active_model=model)
    return templates.TemplateResponse(request, "partials/compare_summary.html", {"report": data})


# ---------------------------------------------------------------- cases


def _case_form(
    request: Request,
    conn: sqlite3.Connection,
    capture: sqlite3.Row,
    rows: list[dict[str, str]],
    *,
    case: cases.Case | None = None,
    changed: bool = False,
    errors: list[str] | None = None,
    status: int = 200,
) -> Response:
    return templates.TemplateResponse(
        request,
        "compare_case.html",
        {
            "capture": capture,
            "photo_id": cases.capture_photo_id(conn, capture["id"]),
            "rows": rows,
            "case": case,
            "changed": changed,
            "errors": errors or [],
            "receipt": capture["type"] == "receipt",
        },
        status_code=status,
    )


def _parse(
    capture: sqlite3.Row,
    name: list[str] | None,
    quantity: list[str] | None,
    price: list[str] | None,
) -> tuple[list[Expected], list[str]]:
    return cases.parse_rows(
        name or [], quantity or [], price or [], with_prices=capture["type"] == "receipt"
    )


def _rows(items: list[Expected]) -> list[dict[str, str]]:
    return [
        {"name": e.name, "quantity": str(e.quantity), "price": format_price(e.price_cents)}
        for e in items
    ]


def _rows_as_typed(
    name: list[str] | None, quantity: list[str] | None, price: list[str] | None
) -> list[dict[str, str]]:
    """The submitted rows, redisplayed as typed after an error."""
    names, quantities, prices = name or [], quantity or [], price or []
    return [
        {
            "name": n,
            "quantity": quantities[i] if i < len(quantities) else "",
            "price": prices[i] if i < len(prices) else "",
        }
        for i, n in enumerate(names)
        if n.strip()
    ]


@router.get("/compare/cases/new")
def new_case(request: Request, conn: Conn, capture_id: int) -> Response:
    """The case form for a finished capture's group (an extension goes to its root),
    pre-filled with the items the group created. A group that already has a live case opens
    that case instead."""
    capture_id = cases.case_capture_id(conn, capture_id)
    capture = service.get(conn, capture_id)
    reason = cases.ineligible_reason(capture)
    if reason:
        return _page_message(request, "Not available", reason, 404 if capture is None else 409)
    existing = cases.for_capture(conn, _settings(request), capture_id)
    if existing is not None and existing.deleted_at is None:
        return RedirectResponse(f"/compare/cases/{existing.id}/edit", status_code=303)
    filled = cases.prefill(conn, capture_id)
    return _case_form(request, conn, capture, _rows(filled.items), changed=filled.changed)


@router.post("/compare/cases")
def create_case(
    request: Request,
    conn: Conn,
    capture_id: Annotated[int, Form()],
    name: FormList = None,
    quantity: FormList = None,
    price: FormList = None,
) -> Response:
    capture_id = cases.case_capture_id(conn, capture_id)
    capture = service.get(conn, capture_id)
    reason = cases.ineligible_reason(capture)
    if reason:
        return _page_message(request, "Not available", reason, 404 if capture is None else 409)
    items, errors = _parse(capture, name, quantity, price)
    if not errors:
        try:
            cases.save(conn, capture_id, items)
        except cases.CaseError as exc:
            errors = [str(exc)]
        else:
            return RedirectResponse("/compare", status_code=303)
    return _case_form(
        request,
        conn,
        capture,
        _rows_as_typed(name, quantity, price),
        errors=errors,
        status=400,
    )


def _live_case(conn: sqlite3.Connection, settings: Settings, case_id: int) -> cases.Case:
    case = cases.get(conn, settings, case_id)
    if case is None or case.deleted_at is not None:
        raise HTTPException(status_code=404, detail="comparison case not found")
    return case


@router.get("/compare/cases/{case_id}/edit")
def edit_case(request: Request, conn: Conn, case_id: int) -> Response:
    case = _live_case(conn, _settings(request), case_id)
    capture = service.get(conn, case.capture_id)
    assert capture is not None
    return _case_form(request, conn, capture, _rows(case.expected), case=case)


@router.post("/compare/cases/{case_id}")
def update_case(
    request: Request,
    conn: Conn,
    case_id: int,
    name: FormList = None,
    quantity: FormList = None,
    price: FormList = None,
) -> Response:
    case = _live_case(conn, _settings(request), case_id)
    capture = service.get(conn, case.capture_id)
    assert capture is not None
    items, errors = _parse(capture, name, quantity, price)
    if not errors and cases.update(conn, case_id, items):
        return RedirectResponse("/compare", status_code=303)
    return _case_form(
        request,
        conn,
        capture,
        _rows_as_typed(name, quantity, price),
        case=case,
        errors=errors or ["This case was deleted meanwhile."],
        status=400,
    )


@router.post("/compare/cases/{case_id}/delete")
def delete_case(request: Request, conn: Conn, case_id: int) -> Response:
    """Soft delete: hidden from new runs, still shown in old reports."""
    if not cases.soft_delete(conn, case_id):
        raise HTTPException(status_code=404, detail="comparison case not found")
    return RedirectResponse("/compare", status_code=303)

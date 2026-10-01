"""Executing comparison results on the capture worker thread (spec C2, R2, R4, R8, R11).

``run_once`` does at most one provider call: claim the next pending result, re-check the
budget, prepare the case's input exactly as a real capture of its type (``prepare_images``
with the same max edge, same prompt mode, same ``known_locations``), call the model, then in
ONE write_tx record the ``ai_calls`` row (with ``comparison_result_id``) and the result
(conditional on it still being pending) and close the run when nothing is pending.

Every model is scored against the case's expected items as snapshotted when the run started
(``comparison_results.expected_json``). Anything that goes wrong after the call returned
(scoring, cost lookup) fails the result but still writes the ledger row. Timeouts are not
retried (OpenRouter may bill them, and they are recorded at an estimate); a rate limit (429)
is retried up to RATE_LIMIT_ATTEMPTS attempts with a growing pause (or the provider's
Retry-After, when longer); other transient errors are retried once. A result's ``error`` is
a plain-language message where there is one; the provider's own message is kept in
``error_detail`` whenever it differs (any failure, and a 429 waiting to retry). While waiting,
the status page computes "trying again in about N s" from ``not_before`` on every poll.
Comparisons never
write items, captures, photos or proposals. A crash between a call returning and its
transaction repeats that call after a restart, until the result has been claimed
``GIVE_UP_AFTER`` times.

Multi-photo cases (spec-multiphoto R9) send every case photo, in order, with the plain shelf
prompt (no existing items, never the extend mode); each prediction's boxes are validated
against the images sent and stored in ``items_json`` as fractions with their photo id (shown
in the report, not scored). The ledger row records the image count.
"""

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from app.ai.provider import (
    AIProvider,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeoutError,
    ProviderTransientError,
    capped_retry_after,
    ledger_outcome,
)
from app.ai.schemas import ExtractRequest, ExtractResult, Mode, boxes_for_images
from app.capture import apply, images
from app.capture.budget import budget_exhausted, cost_micros_for, insert_ai_call, ledger_usage
from app.compare import cases, runs, scoring
from app.config import Settings
from app.db.connection import write_tx
from app.db.timestamps import utcnow

log = logging.getLogger(__name__)

# Same prompt modes as the worker's HANDLERS (test_compare_execution checks they agree).
MODES: dict[str, Mode] = {"photo": "shelf", "receipt": "receipt", "text": "text"}
# A transient error is retried once (attempt 2), later in the run.
MAX_ATTEMPTS = 2
RETRY_DELAY = timedelta(seconds=30)
# A rate limit (429) is retried up to attempt 4, after (at least) these pauses.
RATE_LIMIT_ATTEMPTS = 4
RATE_LIMIT_DELAYS = (timedelta(seconds=30), timedelta(seconds=60), timedelta(seconds=120))
# A provider's Retry-After counts up to this, so a bogus header cannot stall a run.
MAX_RETRY_AFTER = timedelta(minutes=10)
# Claims beyond this (crash loops) fail the result without another call.
GIVE_UP_AFTER = RATE_LIMIT_ATTEMPTS + 2
RATE_LIMITED = "Rate-limited by the model's provider — busy right now; retried {retries} {times}."
RATE_LIMITED_WAITING = "Rate-limited by the model's provider — busy right now; waiting to retry."
NOT_CONFIGURED = "OpenRouter is not configured (OPENROUTER_API_KEY)"

ProviderFor = Callable[[str], AIProvider | None]


def _fail_now(conn: sqlite3.Connection, result: sqlite3.Row, error: str, now: datetime) -> None:
    """Fail a result without a provider call (no ledger row)."""
    with write_tx(conn):
        runs.store_outcome(conn, result["id"], status="failed", now=now, error=error)
        runs.complete_if_done(conn, result["run_id"], now)


def _request(
    conn: sqlite3.Connection, settings: Settings, case: cases.Case
) -> tuple[ExtractRequest, list[int]] | str:
    """The request a real capture of this case's type would send (every case photo, in
    order) and the photo id of each image, or why there is none."""
    mode = MODES.get(case.type)
    if mode is None:
        return f"unsupported capture type {case.type!r}"
    if case.input_missing:
        return cases.INPUT_MISSING
    prepared: list[bytes] = []
    photo_ids: list[int] = []
    for photo_id, path in cases.photo_paths(settings, case):
        try:
            pages = images.prepare_images(path, max_edge=settings.ai_max_image_edge)
        except images.ImageRejected as exc:
            return str(exc)
        prepared += pages
        photo_ids += [photo_id] * len(pages)
    extras: dict[str, Any] = {}
    if case.type == "text":
        extras["known_locations"] = apply.known_location_names(conn)
    request = ExtractRequest(
        mode=mode,
        images=prepared,
        text=case.input_text,
        timeout_s=images.call_timeout(settings.ai_timeout_seconds, len(prepared)),
        **extras,
    )
    return request, photo_ids


def _predictions(
    mode: Mode, result: ExtractResult, image_photo_ids: list[int]
) -> list[dict[str, Any]]:
    """The stored predictions; photo modes add ``boxes`` (validated, as fractions, with the
    image index and photo id), receipts and text never have any."""
    predictions = []
    for item in apply.normalize_predictions(mode, result):
        prediction: dict[str, Any] = {
            "name": item.name,
            "quantity": item.quantity,
            "price_cents": item.price_cents,
        }
        if mode == "shelf":
            prediction["boxes"] = [
                {
                    "image": box.image,
                    "photo_id": image_photo_ids[box.image],
                    "x0": box.x0,
                    "y0": box.y0,
                    "x1": box.x1,
                    "y1": box.y1,
                }
                for box in boxes_for_images(item, len(image_photo_ids))
            ]
        predictions.append(prediction)
    return predictions


def score(
    case: cases.Case, expected: list[scoring.Expected], predictions: list[dict[str, Any]]
) -> dict[str, Any]:
    """``scoring.score_case`` of the predictions against the run's snapshot of the case's
    expected items."""
    predicted = [
        scoring.Predicted(p["name"], p["quantity"], p.get("price_cents")) for p in predictions
    ]
    return scoring.score_case(expected, predicted, receipt=case.type == "receipt")


def _outcome(
    result: sqlite3.Row,
    case: cases.Case,
    mode: Mode,
    extracted: ExtractResult | None,
    error: Exception | None,
    image_photo_ids: list[int],
) -> dict[str, Any]:
    """The result's status, error, items and scores after its call."""
    if extracted is not None:
        predictions = _predictions(mode, extracted, image_photo_ids)
        expected = cases.load_expected(result["expected_json"])
        return {"status": "ok", "items": predictions, "scores": score(case, expected, predictions)}
    attempts = result["attempts"]
    if isinstance(error, ProviderRateLimited):
        if attempts < RATE_LIMIT_ATTEMPTS:
            delay = _delay(RATE_LIMIT_DELAYS[min(attempts, len(RATE_LIMIT_DELAYS)) - 1], error)
            return _failure(error, RATE_LIMITED_WAITING, status="pending", retry_after=delay)
        retries = attempts - 1
        message = RATE_LIMITED.format(retries=retries, times="time" if retries == 1 else "times")
        return _failure(error, message)
    if (
        isinstance(error, ProviderTransientError)
        and not isinstance(error, ProviderTimeoutError)
        and attempts < MAX_ATTEMPTS
    ):
        delay = _delay(RETRY_DELAY, error)
        return _failure(error, str(error), status="pending", retry_after=delay)
    return _failure(error, str(error) or type(error).__name__)


def _failure(
    error: Exception | None, message: str, status: str = "failed", **extra: Any
) -> dict[str, Any]:
    """An outcome with ``message`` and the provider's own message (its ``detail``, else the
    error text) as ``error_detail`` when that differs."""
    detail = error.detail if isinstance(error, ProviderError) and error.detail else str(error)
    return {
        "status": status,
        "error": message,
        "error_detail": detail if detail != message else None,
        **extra,
    }


def _delay(base: timedelta, error: ProviderTransientError) -> timedelta:
    """``base``, or the provider's Retry-After when longer (counted up to MAX_RETRY_AFTER)."""
    return max(base, capped_retry_after(error, MAX_RETRY_AFTER))


def run_once(
    conn: sqlite3.Connection,
    settings: Settings,
    provider_for: ProviderFor,
    now: datetime | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> bool:
    """Execute at most one pending comparison result. False when none was claimable."""
    runs.complete_finished(conn, now or clock())
    result = runs.claim(conn, now or clock())
    if result is None:
        return False
    try:
        _execute(conn, settings, provider_for, result, now, clock)
    except Exception as exc:
        log.exception("comparison result %s failed unexpectedly", result["id"])
        _fail_now(conn, result, f"internal error: {type(exc).__name__}: {exc}", now or clock())
    return True


def _execute(
    conn: sqlite3.Connection,
    settings: Settings,
    provider_for: ProviderFor,
    result: sqlite3.Row,
    now: datetime | None,
    clock: Callable[[], datetime],
) -> None:
    def stamp() -> datetime:
        return now or clock()

    if result["attempts"] > GIVE_UP_AFTER:
        _fail_now(conn, result, f"gave up after {GIVE_UP_AFTER} attempts", stamp())
        return
    if budget_exhausted(conn, settings, stamp()):
        runs.fail_run(conn, result["run_id"], runs.BUDGET_REACHED, stamp())
        return
    case = cases.get(conn, settings, result["case_id"])
    if case is None:
        _fail_now(conn, result, "case not found", stamp())
        return
    prepared = _request(conn, settings, case)
    if isinstance(prepared, str):
        _fail_now(conn, result, prepared, stamp())
        return
    request, image_photo_ids = prepared
    provider = provider_for(result["model"])
    if provider is None:
        _fail_now(conn, result, NOT_CONFIGURED, stamp())
        return

    started_at = stamp()
    started = time.monotonic()
    extracted: ExtractResult | None = None
    error: Exception | None = None
    try:
        extracted = provider.extract(request)
    except Exception as exc:  # any provider failure is recorded, then handled below
        error = exc
    duration_ms = round((time.monotonic() - started) * 1000)
    # The call was made (and maybe billed): from here on nothing may prevent its ledger row.
    usage = extracted.usage if extracted is not None else getattr(error, "usage", None)
    usage, model_info = ledger_usage(conn, settings, provider, usage, error, request)
    counted = (0, 0, 0)
    try:
        if usage is not None:
            cost = cost_micros_for(usage, settings, model_info, provider=provider.name)
            counted = (usage.input_tokens, usage.output_tokens, cost)
        outcome = _outcome(result, case, request.mode, extracted, error, image_photo_ids)
    except Exception as exc:
        log.exception("comparison result %s: processing the answer failed", result["id"])
        outcome = {"status": "failed", "error": f"internal error: {type(exc).__name__}: {exc}"}
    outcome |= {"usage": counted, "duration_ms": duration_ms}

    with write_tx(conn):
        insert_ai_call(
            conn,
            settings,
            None,
            started_at,
            usage,
            ledger_outcome(error),
            comparison_result_id=result["id"],
            model_info=model_info,
            provider=provider.name,
            image_count=len(request.images),
        )
        runs.store_outcome(conn, result["id"], now=stamp(), **outcome)
        runs.complete_if_done(conn, result["run_id"], stamp())

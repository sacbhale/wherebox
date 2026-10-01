"""The single capture worker (plan 3.6 steps 2-7): claim, prepare, call, record, apply.

Exactly one worker thread makes provider calls; that bounds budget overshoot to one in-flight
call. ``run_once`` processes at most one capture and is what tests drive directly
(WORKER_AUTOSTART=false); ``Worker`` runs it in a background thread.

Adding a capture type: one ``Handler`` in ``HANDLERS`` (AI mode, extra request fields, apply
function). An "add another angle" extension (``parent_capture_id`` set; its type stays
``photo``) uses ``EXTEND``. Periodic jobs that share this thread belong in ``Worker._tick``.

One provider call per capture sends all of its images (for an extension: all photos of its
group), in order; ``Applier`` gets the photo id of each image (``image_photo_ids``).
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.ai.model_choice import effective_model
from app.ai.provider import (
    AIProvider,
    ProviderRateLimited,
    ProviderTransientError,
    capped_retry_after,
    ledger_outcome,
)
from app.ai.registry import ai_disabled_reason
from app.ai.schemas import ExtractRequest, ExtractResult, Mode
from app.capture import apply, extend, groups, images, reconcile
from app.capture.budget import budget_exhausted, ledger_usage, record_ai_call
from app.compare import execution as comparisons
from app.config import Settings
from app.db import backup
from app.db.connection import connect, write_tx
from app.db.timestamps import utc_iso, utcnow

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
TRANSIENT_RETRY_DELAY = timedelta(seconds=5)
# A rate limit (429) without Retry-After waits longer: these, by attempt (the last repeats).
RATE_LIMIT_RETRY_DELAYS = (timedelta(seconds=30), timedelta(seconds=60))
# A provider's Retry-After lengthens the delay, up to this.
MAX_RETRY_DELAY = timedelta(minutes=5)
IDLE_WAIT_S = 1.0
# Pause between two processed captures so web requests get the SQLite write lock in between
# (SQLite's lock is not fair; back-to-back write transactions starve waiters).
BUSY_GAP_S = 0.05
BACKUP_RETRY_DELAY = timedelta(minutes=10)

GAVE_UP = f"gave up after {MAX_ATTEMPTS} attempts"
# Safety net behind the group checks of create_capture and retry.
TOO_MANY_GROUP_PHOTOS = f"more than {groups.MAX_GROUP_PHOTOS} photos in this capture"
BUDGET_REACHED = "monthly budget reached"

# (conn, settings, capture, result, image_photo_ids, extras) -> new item ids
Applier = Callable[
    [sqlite3.Connection, Settings, sqlite3.Row, ExtractResult, list[int], dict[str, Any]],
    list[int],
]
RequestExtras = Callable[[sqlite3.Connection, sqlite3.Row], dict[str, Any]]


def _no_extras(conn: sqlite3.Connection, capture: sqlite3.Row) -> dict[str, Any]:
    return {}


def _text_extras(conn: sqlite3.Connection, capture: sqlite3.Row) -> dict[str, Any]:
    return {"known_locations": apply.known_location_names(conn)}


@dataclass(frozen=True)
class Handler:
    """How one capture type is processed.

    ``extras`` adds ExtractRequest fields (it may raise apply.CaptureFailed to fail the
    capture before any provider call); ``apply`` runs inside the write_tx that marks the
    capture done and gets the ``extras`` that were sent."""

    mode: Mode
    extras: RequestExtras
    apply: Applier


HANDLERS: dict[str, Handler] = {
    "photo": Handler("shelf", _no_extras, apply.apply_shelf),
    "receipt": Handler("receipt", _no_extras, apply.apply_receipt),
    "text": Handler("text", _text_extras, apply.apply_text),
    "reconcile": Handler("reconcile", reconcile.request_extras, reconcile.apply_reconcile),
}
# Selected instead of HANDLERS["photo"] when the capture has a parent_capture_id (R5).
EXTEND = Handler("extend", extend.request_extras, extend.apply_extend)


def handler_for(capture: sqlite3.Row) -> Handler | None:
    if capture["type"] == "photo" and capture["parent_capture_id"] is not None:
        return EXTEND
    return HANDLERS.get(capture["type"])


def reset_processing(conn: sqlite3.Connection) -> int:
    """Startup: captures left 'processing' by a stopped worker go back to 'queued' (attempts
    kept, so a crash-looping input still stops after MAX_ATTEMPTS). Returns the count."""
    with write_tx(conn):
        cursor = conn.execute("UPDATE captures SET status = 'queued' WHERE status = 'processing'")
    return cursor.rowcount


def claim(conn: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    """Take the oldest claimable queued capture (not_before <= now) and mark it processing."""
    with write_tx(conn):
        row = conn.execute(
            "UPDATE captures SET status = 'processing', attempts = attempts + 1"
            " WHERE id = (SELECT id FROM captures WHERE status = 'queued' AND not_before <= ?"
            " ORDER BY created_at LIMIT 1) RETURNING id",
            (utc_iso(now),),
        ).fetchone()
    if row is None:
        return None
    return conn.execute("SELECT * FROM captures WHERE id = ?", (row[0],)).fetchone()


def _finish(
    conn: sqlite3.Connection,
    capture_id: int,
    status: str,
    now: datetime,
    error: str | None = None,
    call: dict[str, Any] | None = None,
) -> None:
    """Set a final status. ``call`` holds provider, model and duration_ms when a call ran.
    Call inside write_tx()."""
    call = call or {}
    conn.execute(
        "UPDATE captures SET status = ?, error = ?, finished_at = ?,"
        " provider = coalesce(?, provider), model = coalesce(?, model),"
        " duration_ms = coalesce(?, duration_ms) WHERE id = ?",
        (
            status,
            error,
            utc_iso(now),
            call.get("provider"),
            call.get("model"),
            call.get("duration_ms"),
            capture_id,
        ),
    )


def _fail(
    conn: sqlite3.Connection,
    capture_id: int,
    error: str,
    now: datetime,
    call: dict[str, Any] | None = None,
) -> None:
    with write_tx(conn):
        _finish(conn, capture_id, "failed", now, error, call)


def retry_delay(error: ProviderTransientError, attempts: int = 1) -> timedelta:
    """How long a capture waits after a transient error of its ``attempts``-th call:
    TRANSIENT_RETRY_DELAY (a rate limit without Retry-After: RATE_LIMIT_RETRY_DELAYS), or the
    provider's Retry-After when longer, at most MAX_RETRY_DELAY."""
    base = TRANSIENT_RETRY_DELAY
    if isinstance(error, ProviderRateLimited) and error.retry_after is None:
        base = RATE_LIMIT_RETRY_DELAYS[min(max(attempts, 1), len(RATE_LIMIT_RETRY_DELAYS)) - 1]
    return min(max(base, capped_retry_after(error, MAX_RETRY_DELAY)), MAX_RETRY_DELAY)


def _requeue(
    conn: sqlite3.Connection,
    capture_id: int,
    error: ProviderTransientError,
    now: datetime,
    call: dict[str, Any],
    attempts: int,
) -> None:
    with write_tx(conn):
        conn.execute(
            "UPDATE captures SET status = 'queued', not_before = ?, error = ?, provider = ?,"
            " model = ?, duration_ms = ? WHERE id = ?",
            (
                utc_iso(now + retry_delay(error, attempts)),
                str(error),
                call["provider"],
                call["model"],
                call["duration_ms"],
                capture_id,
            ),
        )


def _prepare(
    conn: sqlite3.Connection, settings: Settings, photos: list[sqlite3.Row]
) -> list[tuple[int, bytes]]:
    """(photo id, JPEG) for every image the provider gets, in the photos' order (a PDF gives
    up to 3 pages of the same photo). A photo without a display JPEG gets one first (upright,
    long edge 2000) and its width/height become the display size; the AI image is then made
    from the display JPEG, so boxes map 1:1 onto it (images module docstring). Old photos
    whose display JPEG is gone are prepared from the original. Raises images.ImageRejected."""
    prepared: list[tuple[int, bytes]] = []
    updates: list[tuple[str, int, int, int]] = []
    try:
        for photo in photos:
            source = settings.data_dir / photo["rel_path"]
            display_rel = photo["display_rel_path"]
            if display_rel is None and not images._is_pdf(source):
                display_rel = Path(photo["rel_path"]).with_suffix(".display.jpg").as_posix()
                width, height = images.make_display_jpeg(source, settings.data_dir / display_rel)
                updates.append((display_rel, width, height, photo["id"]))
            display = settings.data_dir / display_rel if display_rel else None
            path = display if display is not None and display.is_file() else source
            prepared += [
                (photo["id"], jpeg)
                for jpeg in images.prepare_images(path, max_edge=settings.ai_max_image_edge)
            ]
    finally:
        # Display JPEGs already written are recorded even when a later photo is rejected.
        if updates:
            with write_tx(conn):
                conn.executemany(
                    "UPDATE photos SET display_rel_path = ?, width = ?, height = ? WHERE id = ?",
                    updates,
                )
    return prepared


def run_once(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: AIProvider | None,
    now: datetime | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> bool:
    """Process at most one capture. Returns False when nothing was claimable.

    ``now`` fixes the claim time (tests); later timestamps come from ``clock``. An unexpected
    error after the claim marks the capture failed, so it never stays 'processing'.
    """
    capture = claim(conn, now or clock())
    if capture is None:
        return False
    try:
        _process(conn, settings, provider, capture, now, clock)
    except Exception as exc:
        log.exception("capture %s failed unexpectedly", capture["id"])
        _fail(conn, capture["id"], f"internal error: {type(exc).__name__}: {exc}", now or clock())
    return True


def _process(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: AIProvider | None,
    capture: sqlite3.Row,
    now: datetime | None,
    clock: Callable[[], datetime],
) -> None:
    """Everything after the claim. Handled outcomes are recorded here; the ai_calls ledger row
    is written right after the provider call, before anything else."""
    capture_id = capture["id"]

    def stamp() -> datetime:
        return now or clock()

    if capture["attempts"] > MAX_ATTEMPTS:
        _fail(conn, capture_id, GAVE_UP, stamp())
        return
    handler = handler_for(capture)
    if handler is None:
        _fail(conn, capture_id, f"unsupported capture type {capture['type']!r}", stamp())
        return
    if provider is None:
        reason = ai_disabled_reason(settings, effective_model(conn, settings))
        _fail(conn, capture_id, reason or "AI capture is disabled", stamp())
        return

    photos = groups.call_photos(conn, capture)
    if len(photos) > groups.MAX_GROUP_PHOTOS:
        _fail(conn, capture_id, TOO_MANY_GROUP_PHOTOS, stamp())
        return
    try:
        prepared = _prepare(conn, settings, photos)
        extras = handler.extras(conn, capture)
    except (images.ImageRejected, apply.CaptureFailed) as exc:
        _fail(conn, capture_id, str(exc), stamp())
        return

    if budget_exhausted(conn, settings, stamp()):
        _fail(conn, capture_id, BUDGET_REACHED, stamp())
        return

    image_photo_ids = [photo_id for photo_id, _jpeg in prepared]
    request = ExtractRequest(
        mode=handler.mode,
        images=[jpeg for _photo_id, jpeg in prepared],
        text=capture["input_text"],
        timeout_s=images.call_timeout(settings.ai_timeout_seconds, len(prepared)),
        **extras,
    )
    started_at = stamp()
    started = time.monotonic()
    result: ExtractResult | None = None
    error: Exception | None = None
    try:
        result = provider.extract(request)
    except Exception as exc:  # any provider failure is recorded, then handled below
        error = exc
    call = {
        "provider": provider.name,
        "model": provider.model,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
    usage = result.usage if result is not None else getattr(error, "usage", None)
    usage, model_info = ledger_usage(conn, settings, provider, usage, error, request)
    record_ai_call(
        conn,
        settings,
        capture_id,
        started_at,
        usage,
        ledger_outcome(error),
        model_info=model_info,
        provider=provider.name,
        image_count=len(request.images),
    )

    if isinstance(error, ProviderTransientError):
        _requeue(conn, capture_id, error, stamp(), call, capture["attempts"])
        return
    if result is None:
        _fail(conn, capture_id, str(error) or type(error).__name__, stamp(), call)
        return
    try:
        with write_tx(conn):
            handler.apply(conn, settings, capture, result, image_photo_ids, extras)
            _finish(conn, capture_id, "done", stamp(), call=call)
    except apply.CaptureFailed as exc:
        _fail(conn, capture_id, str(exc), stamp(), call)
    except Exception as exc:
        log.exception("applying capture %s failed", capture_id)
        _fail(conn, capture_id, f"could not save the result: {exc}", stamp(), call)


ProviderFor = Callable[[str], AIProvider | None]


class Worker:
    """Background thread around ``run_once``. It idles with ``Event.wait`` (never a busy
    loop); ``wake()`` lets a new capture start without waiting for the idle timeout.

    ``provider_for(model_id)`` (memoized, see ``registry.make_provider_for``) is resolved on
    every tick with the effective model, so a model chosen in Settings applies to the next
    capture. Without it the fixed ``provider`` is used for every model (tests).

    When no capture is claimable, a tick executes one pending model-comparison result with
    ``compare_provider_for(result.model)`` (the OpenRouter factory, or the fake one under
    AI_PROVIDER=fake; default: ``provider_for``), so a comparison delays a new capture by at
    most one call."""

    def __init__(
        self,
        settings: Settings,
        provider: AIProvider | None = None,
        clock: Callable[[], datetime] = utcnow,
        *,
        provider_for: ProviderFor | None = None,
        compare_provider_for: ProviderFor | None = None,
    ) -> None:
        self.settings = settings
        self.provider_for: ProviderFor = provider_for or (lambda _model: provider)
        self.compare_provider_for: ProviderFor = compare_provider_for or self.provider_for
        self.clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._backup_retry_at: datetime | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="capture-worker", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def _tick(self, conn: sqlite3.Connection) -> bool:
        """One unit of background work (captures first, else one comparison call); True
        when something was done."""
        self._backup_if_due(conn)
        provider = self.provider_for(effective_model(conn, self.settings))
        if run_once(conn, self.settings, provider, clock=self.clock):
            return True
        return comparisons.run_once(
            conn, self.settings, self.compare_provider_for, clock=self.clock
        )

    def _backup_if_due(self, conn: sqlite3.Connection) -> None:
        """Every BACKUP_INTERVAL_HOURS (and on the first tick when no recent backup exists)
        write backups/latest.db. It runs between captures, never inside a transaction."""
        now = self.clock()
        if self._backup_retry_at is not None and now < self._backup_retry_at:
            return
        try:
            if backup.run_if_due(conn, self.settings, now):
                log.info("backup written to %s", backup.latest_path(self.settings))
            self._backup_retry_at = None
        except Exception:
            log.exception("backup failed; retrying in %s", BACKUP_RETRY_DELAY)
            self._backup_retry_at = now + BACKUP_RETRY_DELAY

    def _run(self) -> None:
        conn = connect(self.settings.db_path)
        try:
            while not self._stop.is_set():
                try:
                    worked = self._tick(conn)
                except Exception:
                    log.exception("capture worker iteration failed")
                    worked = False
                if worked:
                    self._stop.wait(BUSY_GAP_S)
                else:
                    self._wake.wait(IDLE_WAIT_S)
                    self._wake.clear()
        finally:
            conn.close()

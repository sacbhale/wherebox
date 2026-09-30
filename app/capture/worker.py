"""The single capture worker (plan 3.6 steps 2-7): claim, prepare, call, record, apply.

Exactly one worker thread makes provider calls; that bounds budget overshoot to one in-flight
call. ``run_once`` processes at most one capture and is what tests drive directly
(WORKER_AUTOSTART=false); ``Worker`` runs it in a background thread.

Adding a capture type: one ``Handler`` in ``HANDLERS`` (AI mode, extra request fields, apply
function). Periodic jobs that share this thread belong in ``Worker._tick``.
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

from PIL import Image

from app.ai.provider import AIProvider, ProviderTransientError, ledger_outcome
from app.ai.registry import ai_disabled_reason
from app.ai.schemas import ExtractRequest, ExtractResult, Mode
from app.capture import apply, images, reconcile
from app.capture.budget import budget_exhausted, record_ai_call
from app.config import Settings
from app.db import backup
from app.db.connection import connect, write_tx
from app.db.timestamps import utc_iso, utcnow

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
TRANSIENT_RETRY_DELAY = timedelta(seconds=5)
IDLE_WAIT_S = 1.0
# Pause between two processed captures so web requests get the SQLite write lock in between
# (SQLite's lock is not fair; back-to-back write transactions starve waiters).
BUSY_GAP_S = 0.05
BACKUP_RETRY_DELAY = timedelta(minutes=10)
HEIF_EXTENSIONS = (".heic", ".heif")
EXIF_ORIENTATION = 0x0112

GAVE_UP = f"gave up after {MAX_ATTEMPTS} attempts"
BUDGET_REACHED = "monthly budget reached"

Applier = Callable[
    [sqlite3.Connection, Settings, sqlite3.Row, ExtractResult, int | None, dict[str, Any]],
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


def _requeue(
    conn: sqlite3.Connection, capture_id: int, error: str, now: datetime, call: dict[str, Any]
) -> None:
    with write_tx(conn):
        conn.execute(
            "UPDATE captures SET status = 'queued', not_before = ?, error = ?, provider = ?,"
            " model = ?, duration_ms = ? WHERE id = ?",
            (
                utc_iso(now + TRANSIENT_RETRY_DELAY),
                error,
                call["provider"],
                call["model"],
                call["duration_ms"],
                capture_id,
            ),
        )


def _upright_size(path: Path) -> tuple[int, int] | None:
    """Pixel size after EXIF rotation, from the header only (no decode)."""
    try:
        with Image.open(path) as im:
            width, height = im.size
            if im.getexif().get(EXIF_ORIENTATION) in (5, 6, 7, 8):
                return height, width
            return width, height
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def _prepare(conn: sqlite3.Connection, settings: Settings, photo: sqlite3.Row) -> list[bytes]:
    """Downscaled JPEGs for the provider; also fills the photo's size and, for HEIC, writes
    the display JPEG next to the original. Raises images.ImageRejected."""
    source = settings.data_dir / photo["rel_path"]
    prepared = images.prepare_images(source, max_edge=settings.ai_max_image_edge)
    display_rel = photo["display_rel_path"]
    size: tuple[int, int] | None
    if source.suffix.lower() in HEIF_EXTENSIONS:
        display_rel = display_rel or Path(photo["rel_path"]).with_suffix(".display.jpg").as_posix()
        size = images.make_display_jpeg(source, settings.data_dir / display_rel)
    elif source.suffix.lower() == ".pdf":
        size = None
    else:
        size = _upright_size(source)
    with write_tx(conn):
        conn.execute(
            "UPDATE photos SET display_rel_path = ?, width = ?, height = ? WHERE id = ?",
            (display_rel, size[0] if size else None, size[1] if size else None, photo["id"]),
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
    handler = HANDLERS.get(capture["type"])
    if handler is None:
        _fail(conn, capture_id, f"unsupported capture type {capture['type']!r}", stamp())
        return
    if provider is None:
        _fail(conn, capture_id, ai_disabled_reason(settings) or "AI capture is disabled", stamp())
        return

    photo = conn.execute(
        "SELECT * FROM photos WHERE capture_id = ? ORDER BY id LIMIT 1", (capture_id,)
    ).fetchone()
    try:
        prepared = _prepare(conn, settings, photo) if photo is not None else []
        extras = handler.extras(conn, capture)
    except (images.ImageRejected, apply.CaptureFailed) as exc:
        _fail(conn, capture_id, str(exc), stamp())
        return

    if budget_exhausted(conn, settings, stamp()):
        _fail(conn, capture_id, BUDGET_REACHED, stamp())
        return

    request = ExtractRequest(
        mode=handler.mode,
        images=prepared,
        text=capture["input_text"],
        timeout_s=settings.ai_timeout_seconds,
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
    record_ai_call(conn, settings, capture_id, started_at, usage, ledger_outcome(error))

    if isinstance(error, ProviderTransientError):
        _requeue(conn, capture_id, str(error), stamp(), call)
        return
    if result is None:
        _fail(conn, capture_id, str(error) or type(error).__name__, stamp(), call)
        return
    try:
        with write_tx(conn):
            handler.apply(conn, settings, capture, result, photo["id"] if photo else None, extras)
            _finish(conn, capture_id, "done", stamp(), call=call)
    except apply.CaptureFailed as exc:
        _fail(conn, capture_id, str(exc), stamp(), call)
    except Exception as exc:
        log.exception("applying capture %s failed", capture_id)
        _fail(conn, capture_id, f"could not save the result: {exc}", stamp(), call)


class Worker:
    """Background thread around ``run_once``. It idles with ``Event.wait`` (never a busy
    loop); ``wake()`` lets a new capture start without waiting for the idle timeout."""

    def __init__(
        self,
        settings: Settings,
        provider: AIProvider | None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.provider = provider
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
        """One unit of background work; True when something was done."""
        self._backup_if_due(conn)
        return run_once(conn, self.settings, self.provider, clock=self.clock)

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

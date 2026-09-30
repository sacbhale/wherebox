"""Capture requests (plan 3.6 step 1): save uploads, queue captures, queue position, retry.

Uploads are stored untouched at ``DATA_DIR/photos/YYYY/MM/<uuid>.<ext>``; ``photos.rel_path``
is relative to DATA_DIR (``photos/2026/09/<uuid>.jpg``). No image is decoded here: the
worker's prepare step does that.
"""

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from app.ai.registry import ai_disabled_reason
from app.capture.budget import budget_exhausted
from app.config import Settings
from app.db.connection import write_tx
from app.db.timestamps import utc_iso

CAPTURE_TYPES = ("photo", "receipt", "text", "reconcile")
BUDGET_REACHED = "Monthly AI budget reached: AI capture is paused until next month."
ALLOWED_EXTENSIONS = frozenset(
    {"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "tif", "tiff", "bmp", "pdf"}
)
_CHUNK = 1024 * 1024
# A capture with ``item_count`` (items it created) and its location's name, code and deleted_at.
_SELECT = (
    "SELECT c.*, l.name AS location_name, l.short_code AS location_code,"
    " l.deleted_at AS location_deleted_at,"
    " (SELECT count(*) FROM items i WHERE i.capture_id = c.id) AS item_count"
    " FROM captures c LEFT JOIN locations l ON l.id = c.location_id"
)


class UploadError(ValueError):
    """A rejected upload: ``status`` is the HTTP status to answer with (400 or 413)."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SavedUpload:
    rel_path: str
    sha256: str
    size: int


def capture_blocked_reason(
    conn: sqlite3.Connection, settings: Settings, now: datetime
) -> str | None:
    """Why new AI captures are refused right now (AI disabled or budget reached), or None."""
    disabled = ai_disabled_reason(settings)
    if disabled:
        return disabled
    if budget_exhausted(conn, settings, now):
        return BUDGET_REACHED
    return None


def _sniff_extension(head: bytes) -> str | None:
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"mif1"):
        return "heic"
    return None


def _extension(filename: str | None, head: bytes) -> str:
    suffix = Path(filename or "").suffix.lower().lstrip(".")
    if suffix in ALLOWED_EXTENSIONS:
        return suffix
    return _sniff_extension(head) or "bin"


def save_upload(
    settings: Settings, source: BinaryIO, filename: str | None, now: datetime
) -> SavedUpload:
    """Copy an upload stream to the photos directory, hashing it and enforcing MAX_UPLOAD_MB.

    Raises UploadError (empty: 400, too large: 413); nothing is left on disk then.
    """
    limit = settings.max_upload_mb * 1024 * 1024
    head = source.read(16)
    if not head:
        raise UploadError("Choose a photo or file to upload.")
    name = f"{uuid.uuid4().hex}.{_extension(filename, head)}"
    rel = Path("photos") / f"{now:%Y}" / f"{now:%m}" / name
    target = settings.data_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with target.open("xb") as out:
            chunk = head
            while chunk:
                size += len(chunk)
                if size > limit:
                    raise UploadError(
                        f"The file is larger than the {settings.max_upload_mb} MB limit.", 413
                    )
                digest.update(chunk)
                out.write(chunk)
                chunk = source.read(_CHUNK)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return SavedUpload(rel_path=rel.as_posix(), sha256=digest.hexdigest(), size=size)


def discard_upload(settings: Settings, upload: SavedUpload) -> None:
    (settings.data_dir / upload.rel_path).unlink(missing_ok=True)


def create_capture(
    conn: sqlite3.Connection,
    *,
    capture_type: str,
    now: datetime,
    location_id: int | None = None,
    input_text: str | None = None,
    upload: SavedUpload | None = None,
) -> int:
    """Queue a capture (status 'queued', not_before = now) and its photo row in one write_tx."""
    if capture_type not in CAPTURE_TYPES:
        raise ValueError(f"unknown capture type {capture_type!r}")
    stamp = utc_iso(now)
    with write_tx(conn):
        capture_id = conn.execute(
            "INSERT INTO captures (type, status, location_id, input_text, not_before, created_at)"
            " VALUES (?, 'queued', ?, ?, ?, ?)",
            (capture_type, location_id, input_text, stamp, stamp),
        ).lastrowid
        if upload is not None:
            conn.execute(
                "INSERT INTO photos (capture_id, rel_path, sha256, created_at) VALUES (?, ?, ?, ?)",
                (capture_id, upload.rel_path, upload.sha256, stamp),
            )
    assert capture_id is not None
    return capture_id


def get(conn: sqlite3.Connection, capture_id: int) -> sqlite3.Row | None:
    return conn.execute(_SELECT + " WHERE c.id = ?", (capture_id,)).fetchone()


def recent(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Newest captures first, shaped like ``get``."""
    return conn.execute(
        _SELECT + " ORDER BY c.created_at DESC, c.id DESC LIMIT ?", (limit,)
    ).fetchall()


def queue_position(conn: sqlite3.Connection, capture: sqlite3.Row) -> int:
    """How many unfinished captures the single worker handles before this one (AC-15)."""
    return conn.execute(
        "SELECT count(*) FROM captures WHERE status IN ('queued', 'processing') AND id != ?"
        " AND (status = 'processing' OR created_at < ? OR (created_at = ? AND id < ?))",
        (capture["id"], capture["created_at"], capture["created_at"], capture["id"]),
    ).fetchone()[0]


def retry(conn: sqlite3.Connection, capture_id: int, now: datetime) -> bool:
    """Re-queue a failed capture from scratch (attempts=0). False if it is not failed."""
    with write_tx(conn):
        cursor = conn.execute(
            "UPDATE captures SET status = 'queued', attempts = 0, not_before = ?, error = NULL,"
            " finished_at = NULL WHERE id = ? AND status = 'failed'",
            (utc_iso(now), capture_id),
        )
    return cursor.rowcount == 1


def active(conn: sqlite3.Connection, location_id: int | None = None) -> list[sqlite3.Row]:
    """Unfinished captures (oldest first), optionally only those for one location."""
    return conn.execute(
        "SELECT * FROM captures WHERE status IN ('queued', 'processing')"
        " AND (? IS NULL OR location_id = ?) ORDER BY created_at, id",
        (location_id, location_id),
    ).fetchall()


def outcome(conn: sqlite3.Connection, capture_id: int) -> list[sqlite3.Row]:
    """Where a capture's items went: per location, name, code, item count and review count."""
    return conn.execute(
        "SELECT l.name, l.short_code, count(*) AS n, sum(i.needs_review) AS review"
        " FROM items i JOIN locations l ON l.id = i.location_id"
        " WHERE i.capture_id = ? GROUP BY l.id ORDER BY l.name COLLATE NOCASE",
        (capture_id,),
    ).fetchall()

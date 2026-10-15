"""Capture requests (plan 3.6 step 1): save uploads, queue captures, queue position, retry.

Uploads are stored untouched at ``DATA_DIR/photos/YYYY/MM/<uuid>.<ext>``; ``photos.rel_path``
is relative to DATA_DIR (``photos/2026/09/<uuid>.jpg``). No image is decoded here: the
worker's prepare step does that.

Photo and reconcile captures take 1-6 photos per submission (saved all-or-nothing, in
submission order = ``photos.position``); an "add another angle" extension adds photos to a
done photo capture's group (at most 12 photos per group, checked in the same write_tx as
the inserts).
"""

import hashlib
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

from app.ai.model_choice import effective_model
from app.ai.registry import ai_disabled_reason
from app.capture import groups
from app.capture.budget import budget_exhausted
from app.config import Settings
from app.db.connection import write_tx
from app.db.timestamps import utc_iso

CAPTURE_TYPES = ("photo", "receipt", "text", "reconcile")
BUDGET_REACHED = "Monthly AI budget reached: AI capture is paused until next month."
# Kept as the stored file's extension; anything else is stored as its sniffed type or
# ".bin" (and refused by the worker, which only decodes images.PHOTO_FORMATS and PDF).
ALLOWED_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "heic", "heif", "pdf"})
_CHUNK = 1024 * 1024
PDF_NOT_A_PHOTO = "Use a photo; PDFs are for receipts."
DUPLICATE_PHOTO = "That photo is already in this capture."
# Retry of a failed extension that no longer fits its group (R7: the group changed since).
RETRY_GROUP_FULL = "This capture is full now — add the photos as a new angle."
RETRY_DUPLICATE = "Those photos are already in this capture."
# A capture with ``item_count`` (items it created), ``photo_count`` and its location's name,
# code and deleted_at.
_SELECT = (
    "SELECT c.*, l.name AS location_name, l.short_code AS location_code,"
    " l.deleted_at AS location_deleted_at,"
    " (SELECT count(*) FROM items i WHERE i.capture_id = c.id) AS item_count,"
    " (SELECT count(*) FROM photos p WHERE p.capture_id = c.id) AS photo_count"
    " FROM captures c LEFT JOIN locations l ON l.id = c.location_id"
)


class RetryRefused(ValueError):
    """A failed capture that cannot be re-queued as it is (the message says why)."""


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
    disabled = ai_disabled_reason(settings, effective_model(conn, settings))
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
    settings: Settings,
    source: BinaryIO,
    filename: str | None,
    now: datetime,
    *,
    allow_pdf: bool = True,
) -> SavedUpload:
    """Copy an upload stream to the photos directory, hashing it and enforcing MAX_UPLOAD_MB.

    Raises UploadError (empty or a PDF when ``allow_pdf`` is false: 400, too large: 413);
    nothing is left on disk then.
    """
    limit = settings.max_upload_mb * 1024 * 1024
    head = source.read(16)
    if not head:
        raise UploadError("Choose a photo or file to upload.")
    if not allow_pdf and (
        _sniff_extension(head) == "pdf" or Path(filename or "").suffix.lower() == ".pdf"
    ):
        raise UploadError(PDF_NOT_A_PHOTO)
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


def save_uploads(
    settings: Settings,
    files: Sequence[tuple[BinaryIO, str | None]],
    now: datetime,
    *,
    allow_pdf: bool = True,
) -> list[SavedUpload]:
    """Save several uploads, all or nothing: when one is rejected (UploadError, including a
    file whose content repeats an earlier one of the submission), the ones already saved are
    removed before the error propagates."""
    saved: list[SavedUpload] = []
    try:
        for source, filename in files:
            upload = save_upload(settings, source, filename, now, allow_pdf=allow_pdf)
            saved.append(upload)
            if any(earlier.sha256 == upload.sha256 for earlier in saved[:-1]):
                raise UploadError(DUPLICATE_PHOTO)
    except BaseException:
        for upload in saved:
            discard_upload(settings, upload)
        raise
    return saved


def discard_upload(settings: Settings, upload: SavedUpload) -> None:
    (settings.data_dir / upload.rel_path).unlink(missing_ok=True)


def _extension_target(
    conn: sqlite3.Connection, extends: int, uploads: Sequence[SavedUpload]
) -> sqlite3.Row:
    """Inside create_capture's write_tx: the root capture a new extension of ``extends``
    attaches to, after checking the target (done photo capture), the 12-photo group limit
    (queued, processing and done captures count) and repeated photos. Raises UploadError."""
    target = conn.execute("SELECT * FROM captures WHERE id = ?", (extends,)).fetchone()
    refusal = groups.extend_refusal(target)
    if refusal:
        raise UploadError(refusal, 404 if target is None else 409)
    root_capture_id = groups.root_id(target)
    root = conn.execute("SELECT * FROM captures WHERE id = ?", (root_capture_id,)).fetchone()
    counted = groups.counted_photos(conn, root_capture_id)
    room = groups.MAX_GROUP_PHOTOS - len(counted)
    if len(uploads) > room:
        raise UploadError(
            f"A capture holds at most {groups.MAX_GROUP_PHOTOS} photos: "
            + (f"there is room for {room} more." if room > 0 else "this one is full.")
        )
    if {row["sha256"] for row in counted} & {upload.sha256 for upload in uploads}:
        raise UploadError(DUPLICATE_PHOTO)
    return root


def create_capture(
    conn: sqlite3.Connection,
    *,
    capture_type: str,
    now: datetime,
    location_id: int | None = None,
    input_text: str | None = None,
    upload: SavedUpload | None = None,
    uploads: Sequence[SavedUpload] = (),
    extends: int | None = None,
) -> int:
    """Queue a capture (status 'queued', not_before = now) and its photo rows (``upload``,
    or ``uploads`` in submission order = ``photos.position``) in one write_tx.

    ``extends`` makes an "add another angle" extension of that capture: a ``photo`` capture
    whose ``parent_capture_id`` is the group's root and whose location is the root's
    (``location_id`` is ignored). Its checks run in the same write_tx as the inserts, so
    concurrent submissions cannot exceed the group limit; they raise UploadError (nothing
    is written then; the caller discards the saved files)."""
    if capture_type not in CAPTURE_TYPES:
        raise ValueError(f"unknown capture type {capture_type!r}")
    files = [*([upload] if upload is not None else []), *uploads]
    stamp = utc_iso(now)
    parent_capture_id = None
    with write_tx(conn):
        if extends is not None:
            root = _extension_target(conn, extends, files)
            parent_capture_id, location_id, capture_type = root["id"], root["location_id"], "photo"
        capture_id = conn.execute(
            "INSERT INTO captures (type, status, location_id, input_text, not_before, created_at,"
            " parent_capture_id) VALUES (?, 'queued', ?, ?, ?, ?, ?)",
            (capture_type, location_id, input_text, stamp, stamp, parent_capture_id),
        ).lastrowid
        conn.executemany(
            "INSERT INTO photos (capture_id, rel_path, sha256, position, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (capture_id, saved.rel_path, saved.sha256, position, stamp)
                for position, saved in enumerate(files)
            ],
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


def _retry_refusal(conn: sqlite3.Connection, capture: sqlite3.Row) -> str | None:
    """Inside retry's write_tx, for a failed extension: why re-queueing it would break the
    group rules (its photos stopped counting when it failed, so the group may have filled up
    or received the same photos since), or None."""
    root = groups.root_id(capture)
    counted = [
        row for row in groups.counted_photos(conn, root) if row["capture_id"] != capture["id"]
    ]
    own = conn.execute(
        "SELECT sha256 FROM photos WHERE capture_id = ?", (capture["id"],)
    ).fetchall()
    if len(counted) + len(own) > groups.MAX_GROUP_PHOTOS:
        return RETRY_GROUP_FULL
    if {row["sha256"] for row in counted} & {row["sha256"] for row in own}:
        return RETRY_DUPLICATE
    return None


def retry(conn: sqlite3.Connection, capture_id: int, now: datetime) -> bool:
    """Re-queue a failed capture from scratch (attempts=0). False if it is not failed.

    A failed extension is re-checked against its group in the same write_tx (group limit,
    repeated photos); RetryRefused (nothing changes) when it no longer fits."""
    with write_tx(conn):
        capture = conn.execute(
            "SELECT * FROM captures WHERE id = ? AND status = 'failed'", (capture_id,)
        ).fetchone()
        if capture is None:
            return False
        if capture["parent_capture_id"] is not None:
            refusal = _retry_refusal(conn, capture)
            if refusal:
                raise RetryRefused(refusal)
        conn.execute(
            "UPDATE captures SET status = 'queued', attempts = 0, not_before = ?, error = NULL,"
            " finished_at = NULL WHERE id = ?",
            (utc_iso(now), capture_id),
        )
    return True


def active(conn: sqlite3.Connection, location_id: int | None = None) -> list[sqlite3.Row]:
    """Unfinished captures (oldest first), optionally only those for one location."""
    return conn.execute(
        "SELECT * FROM captures WHERE status IN ('queued', 'processing')"
        " AND (? IS NULL OR location_id = ?) ORDER BY created_at, id",
        (location_id, location_id),
    ).fetchall()


def outcome(conn: sqlite3.Connection, capture_id: int) -> list[sqlite3.Row]:
    """Where a capture's items went: per location, name, code, item count and review count.
    Items deleted as wrong detections do not count; used-up ones do."""
    return conn.execute(
        "SELECT l.name, l.short_code, count(*) AS n, sum(i.needs_review) AS review"
        " FROM items i JOIN locations l ON l.id = i.location_id"
        " WHERE i.capture_id = ? AND i.deleted_reason IS NOT 'deleted'"
        " GROUP BY l.id ORDER BY l.name COLLATE NOCASE",
        (capture_id,),
    ).fetchall()

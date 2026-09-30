"""Consistent database snapshots in ``backups/latest.db`` and the integrity check / repair.

``write_backup`` runs ``VACUUM INTO`` (a read transaction: in WAL mode it does not block
writers) into ``latest.db.tmp``, fsyncs it and atomically renames it over ``latest.db``, so
``latest.db`` is always either the previous or the new complete snapshot. The worker calls
``run_if_due`` on every tick; ``python -m app.cli backup`` calls ``write_backup`` directly.
"""

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.db.connection import write_tx
from app.db.timestamps import parse_utc, utc_iso

LATEST = "latest.db"
TMP_SUFFIX = ".tmp"


def latest_path(settings: Settings) -> Path:
    return settings.backups_dir / LATEST


def last_backup_time(conn: sqlite3.Connection) -> datetime | None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'last_backup_at'").fetchone()
    return parse_utc(row[0]) if row else None


def is_due(conn: sqlite3.Connection, settings: Settings, now: datetime) -> bool:
    """True when no backup was recorded, ``latest.db`` is missing, or the last backup is
    ``BACKUP_INTERVAL_HOURS`` old (a clock set back far also counts as due)."""
    last = last_backup_time(conn)
    if last is None or not latest_path(settings).exists():
        return True
    age = now - last
    return age >= timedelta(hours=settings.backup_interval_hours) or age < timedelta(hours=-1)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_backup(conn: sqlite3.Connection, settings: Settings, now: datetime) -> Path:
    """Snapshot the database to ``backups/latest.db`` and record ``last_backup_at``.

    Must not be called inside a transaction (VACUUM cannot run in one)."""
    if conn.in_transaction:
        raise RuntimeError("write_backup must not run inside a transaction")
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    target = latest_path(settings)
    tmp = target.with_name(target.name + TMP_SUFFIX)
    tmp.unlink(missing_ok=True)  # VACUUM INTO fails when the target file exists
    try:
        conn.execute("VACUUM INTO ?", (str(tmp),))
        _fsync(tmp)
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync(target.parent)
    with write_tx(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('last_backup_at', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (utc_iso(now),),
        )
    return target


def run_if_due(conn: sqlite3.Connection, settings: Settings, now: datetime) -> bool:
    """Write a backup when one is due. Returns True when a backup was written."""
    if not is_due(conn, settings, now):
        return False
    write_backup(conn, settings, now)
    return True


def integrity_errors(path: Path) -> list[str]:
    """``PRAGMA integrity_check`` of the database at ``path``: [] when it is ok.

    Opened read-only, so checking never creates or modifies the file."""
    if not path.exists():
        return [f"{path} does not exist"]
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = [row[0] for row in conn.execute("PRAGMA integrity_check")]
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return [str(exc)]
    return [] if rows == ["ok"] else rows


def restore_latest(settings: Settings, now: datetime) -> Path:
    """Replace ``inventory.db`` with ``backups/latest.db`` (the app must be stopped).

    The damaged database is kept as ``inventory.db.corrupt-<UTC timestamp>``. Its ``-wal``
    (moved next to it) and ``-shm`` (deleted) are removed first, so SQLite cannot replay
    them onto the backup. Returns the path the damaged database was moved to."""
    db = settings.db_path
    source = latest_path(settings)
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    kept = db.with_name(f"{db.name}.corrupt-{stamp}")
    wal = db.with_name(db.name + "-wal")
    if wal.exists():
        os.replace(wal, kept.with_name(kept.name + "-wal"))
    db.with_name(db.name + "-shm").unlink(missing_ok=True)
    tmp = db.with_name(db.name + TMP_SUFFIX)
    tmp.write_bytes(source.read_bytes())
    _fsync(tmp)
    if db.exists():
        os.replace(db, kept)
    os.replace(tmp, db)
    _fsync(db.parent)
    return kept

"""Step 6 backups: the worker's backup tick, `cli backup` and `cli check [--repair]`."""

import io
import sqlite3
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import cli
from app.capture.worker import Worker
from app.config import Settings
from app.db import backup
from app.db.connection import connect
from app.db.migrate import migrate
from app.inventory import items, locations
from tests.inventory_helpers import open_db

T0 = datetime(2026, 9, 29, 3, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _seed(conn: sqlite3.Connection) -> int:
    garage, _ = locations.create(conn, "Garage")
    return items.create(conn, items.ItemData(name="Drill", location_id=garage, kind="belonging"))


def _latest(settings: Settings) -> Path:
    return settings.backups_dir / "latest.db"


def _names_in(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as db:
        return [row[0] for row in db.execute("SELECT name FROM items ORDER BY id")]


def _cli(settings: Settings, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv), settings)
    return code, out.getvalue(), err.getvalue()


def test_backup_tick_writes_consistent_snapshot(conn: sqlite3.Connection, settings: Settings):
    _seed(conn)
    stale = settings.backups_dir / "latest.db.tmp"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"left over from a crash")

    worker = Worker(settings, None, clock=Clock(T0))
    assert worker._tick(conn) is False  # no capture was processed

    latest = _latest(settings)
    assert latest.exists()
    assert not stale.exists()
    assert backup.integrity_errors(latest) == []
    assert _names_in(latest) == ["Drill"]
    assert backup.last_backup_time(conn) == T0


def test_backup_interval_respected(conn: sqlite3.Connection, settings: Settings) -> None:
    _seed(conn)
    clock = Clock(T0)
    worker = Worker(settings, None, clock=clock)
    worker._tick(conn)
    first_mtime = _latest(settings).stat().st_mtime_ns

    items.create(conn, items.ItemData(name="Saw", location_id=1, kind="belonging"))
    clock.now = T0 + timedelta(hours=23, minutes=59)
    worker._tick(conn)
    assert _latest(settings).stat().st_mtime_ns == first_mtime
    assert _names_in(_latest(settings)) == ["Drill"]
    assert backup.last_backup_time(conn) == T0

    clock.now = T0 + timedelta(hours=24)
    worker._tick(conn)
    assert _names_in(_latest(settings)) == ["Drill", "Saw"]
    assert backup.last_backup_time(conn) == T0 + timedelta(hours=24)


def test_custom_interval_and_missing_file_make_backup_due(
    make_settings, conn: sqlite3.Connection
) -> None:
    settings = make_settings(backup_interval_hours=2)
    assert backup.is_due(conn, settings, T0)  # never backed up
    backup.write_backup(conn, settings, T0)
    assert not backup.is_due(conn, settings, T0 + timedelta(hours=1, minutes=59))
    assert backup.is_due(conn, settings, T0 + timedelta(hours=2))
    _latest(settings).unlink()
    assert backup.is_due(conn, settings, T0 + timedelta(minutes=1))


def test_backup_refuses_to_run_inside_a_transaction(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    conn.execute("BEGIN")
    try:
        with pytest.raises(RuntimeError):
            backup.write_backup(conn, settings, T0)
    finally:
        conn.execute("ROLLBACK")
    assert not _latest(settings).exists()


def test_failed_backup_does_not_stop_captures_and_retries_later(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[datetime] = []

    def broken(conn: sqlite3.Connection, settings: Settings, now: datetime) -> bool:
        calls.append(now)
        raise OSError("disk full")

    monkeypatch.setattr(backup, "run_if_due", broken)
    clock = Clock(T0)
    worker = Worker(settings, None, clock=clock)
    assert worker._tick(conn) is False
    clock.now = T0 + timedelta(minutes=5)
    worker._tick(conn)
    assert calls == [T0]
    clock.now = T0 + timedelta(minutes=10)
    worker._tick(conn)
    assert calls == [T0, T0 + timedelta(minutes=10)]


def test_last_backup_shown_on_settings_page(auth_client: TestClient, auth_settings: Settings):
    assert "No backup has been written yet." in auth_client.get("/settings").text
    now = datetime.now(UTC)
    with open_db(auth_settings) as conn:
        Worker(auth_settings, None, clock=lambda: now)._tick(conn)
    html = auth_client.get("/settings").text
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    assert f'<dd id="last-backup">{stamp}</dd>' in html
    assert "No backup has been written yet." not in html


def test_cli_backup(conn: sqlite3.Connection, settings: Settings) -> None:
    _seed(conn)
    code, out, _ = _cli(settings, "backup")
    assert code == 0
    assert "latest.db" in out
    assert backup.integrity_errors(_latest(settings)) == []
    assert _names_in(_latest(settings)) == ["Drill"]
    assert backup.last_backup_time(conn) is not None


def test_cli_backup_without_database(settings: Settings) -> None:
    code, _, err = _cli(settings, "backup")
    assert code == 1
    assert "no database" in err
    assert not settings.db_path.exists()


def test_cli_check_ok(conn: sqlite3.Connection, settings: Settings) -> None:
    _seed(conn)
    code, out, _ = _cli(settings, "check")
    assert code == 0
    assert out.startswith("ok:")


def _corrupt(settings: Settings) -> None:
    """Overwrite a run of pages after the header (all connections must be closed)."""
    data = bytearray(settings.db_path.read_bytes())
    page_size = int.from_bytes(data[16:18], "big")
    assert len(data) >= page_size * 6
    data[page_size * 3 : page_size * 6] = b"\x5a" * (page_size * 3)
    settings.db_path.write_bytes(bytes(data))


def _seed_many_and_back_up(settings: Settings) -> None:
    with closing(connect(settings.db_path)) as conn:
        migrate(conn)
        garage, _ = locations.create(conn, "Garage")
        for index in range(200):
            items.create(
                conn,
                items.ItemData(
                    name=f"Item {index}", location_id=garage, kind="belonging", notes="x" * 200
                ),
            )
        backup.write_backup(conn, settings, T0)
        items.create(conn, items.ItemData(name="After backup", location_id=1, kind="belonging"))


def test_cli_check_detects_corruption_and_repairs_only_when_asked(settings: Settings) -> None:
    _seed_many_and_back_up(settings)
    _corrupt(settings)
    wal = settings.db_path.with_name("inventory.db-wal")
    shm = settings.db_path.with_name("inventory.db-shm")
    wal.write_bytes(b"stale wal")
    shm.write_bytes(b"stale shm")

    code, _, err = _cli(settings, "check")
    assert code == 1
    assert "failed PRAGMA integrity_check" in err
    assert "--repair" in err
    assert backup.integrity_errors(settings.db_path) != []  # untouched without --repair

    code, out, _ = _cli(settings, "check", "--repair")
    assert code == 0
    assert "Restored" in out
    assert backup.integrity_errors(settings.db_path) == []
    assert not wal.exists()
    assert not shm.exists()
    names = _names_in(settings.db_path)
    assert len(names) == 200
    assert "After backup" not in names
    kept = list(settings.data_dir.glob("inventory.db.corrupt-*"))
    assert [p.name.endswith("-wal") for p in sorted(kept)] == [False, True]

    code, out, _ = _cli(settings, "check")
    assert code == 0


def test_cli_check_fails_without_usable_backup(settings: Settings) -> None:
    _seed_many_and_back_up(settings)
    _latest(settings).unlink()
    _corrupt(settings)
    code, _, err = _cli(settings, "check", "--repair")
    assert code == 1
    assert "no usable backup" in err
    assert backup.integrity_errors(settings.db_path) != []


def test_cli_check_missing_database(settings: Settings) -> None:
    code, _, err = _cli(settings, "check")
    assert code == 1
    assert "does not exist" in err

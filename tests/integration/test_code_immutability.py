import re
import sqlite3
from pathlib import Path

import pytest

from app.db.connection import write_tx
from app.inventory import shortcode

APP_DIR = Path(__file__).resolve().parents[2] / "app"


@pytest.fixture
def loc(conn):
    with write_tx(conn):
        rowid, code = shortcode.insert_with_code(
            conn,
            "INSERT INTO locations (short_code, name) VALUES (:short_code, :name)",
            {"name": "Garage"},
        )
    return rowid, code


def row(conn, rowid):
    return conn.execute("SELECT short_code, name FROM locations WHERE id = ?", (rowid,)).fetchone()


def test_update_short_code_aborts(conn, loc):
    rowid, code = loc
    with pytest.raises(sqlite3.IntegrityError, match="short_code immutable"), write_tx(conn):
        conn.execute("UPDATE locations SET short_code = 'ZZZZZZ' WHERE id = ?", (rowid,))
    assert row(conn, rowid)["short_code"] == code


def test_update_same_code_and_other_columns_allowed(conn, loc):
    rowid, code = loc
    with write_tx(conn):
        conn.execute(
            "UPDATE locations SET short_code = ?, name = 'Garage 2' WHERE id = ?", (code, rowid)
        )
        conn.execute("UPDATE locations SET deleted_at = 'x' WHERE id = ?", (rowid,))
    assert tuple(row(conn, rowid)) == (code, "Garage 2")


def test_delete_aborts(conn, loc):
    rowid, code = loc
    with pytest.raises(sqlite3.IntegrityError, match="soft-deleted"), write_tx(conn):
        conn.execute("DELETE FROM locations WHERE id = ?", (rowid,))
    assert row(conn, rowid)["short_code"] == code


@pytest.mark.parametrize(
    "sql",
    [
        # conflict on the UNIQUE short_code
        "INSERT OR REPLACE INTO locations (short_code, name) VALUES (:code, 'Impostor')",
        "REPLACE INTO locations (short_code, name) VALUES (:code, 'Impostor')",
        # conflict on the primary key, with a new code
        "INSERT OR REPLACE INTO locations (id, short_code, name) VALUES (:id, 'NEW000', 'X')",
    ],
)
def test_replace_aborts(conn, loc, sql):
    rowid, code = loc
    with pytest.raises(sqlite3.IntegrityError, match="soft-deleted"), write_tx(conn):
        conn.execute(sql, {"code": code, "id": rowid})
    assert tuple(row(conn, rowid)) == (code, "Garage")
    assert conn.execute("SELECT count(*) FROM locations").fetchone()[0] == 2  # Inbox + Garage


def test_inbox_code_protected_too(conn):
    inbox_id = conn.execute("SELECT id FROM locations WHERE is_system = 1").fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError), write_tx(conn):
        conn.execute("UPDATE locations SET short_code = 'ZZZZZZ' WHERE id = ?", (inbox_id,))


BANNED = re.compile(
    r"\b(?:INSERT\s+OR\s+REPLACE\s+INTO|REPLACE\s+INTO|UPDATE\s+OR\s+REPLACE)\s+[\"'`\[]?locations\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "sample",
    [
        "INSERT OR REPLACE INTO locations",
        "replace into locations(",
        'REPLACE INTO "locations"',
        "UPDATE OR REPLACE locations SET",
        "insert or replace\n    into locations",
    ],
)
def test_ban_pattern_matches(sample):
    assert BANNED.search(sample)


def test_no_replace_against_locations_in_app():
    files = [p for p in APP_DIR.rglob("*") if p.suffix in {".py", ".sql"}]
    assert files
    offenders = [
        f"{path.relative_to(APP_DIR)}: {m.group(0)!r}"
        for path in files
        for m in BANNED.finditer(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []

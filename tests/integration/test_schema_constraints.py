import sqlite3

import pytest

from app.db.connection import write_tx


@pytest.fixture
def inbox_id(conn):
    return conn.execute("SELECT id FROM locations WHERE is_system = 1").fetchone()[0]


def add_item(conn, location_id, /, **cols):
    values = {"name": "Milk", "kind": "consumable", "location_id": location_id, **cols}
    keys = ", ".join(values)
    marks = ", ".join(f":{k}" for k in values)
    with write_tx(conn):
        return conn.execute(f"INSERT INTO items ({keys}) VALUES ({marks})", values).lastrowid


def test_item_defaults(conn, inbox_id):
    item = conn.execute(
        "SELECT quantity, needs_review, created_at FROM items WHERE id = ?",
        (add_item(conn, inbox_id),),
    ).fetchone()
    assert (item["quantity"], item["needs_review"]) == (1, 0)
    assert item["created_at"].endswith("Z")


@pytest.mark.parametrize(
    "cols", [{"quantity": -1}, {"kind": "gadget"}, {"location_id": 999_999}, {"name": None}]
)
def test_item_constraints(conn, inbox_id, cols):
    with pytest.raises(sqlite3.IntegrityError):
        add_item(conn, inbox_id, **cols)
    assert not conn.in_transaction


def test_category_names_unique_case_insensitive(conn):
    with write_tx(conn):
        conn.execute("INSERT INTO categories (name) VALUES ('Tools')")
    with pytest.raises(sqlite3.IntegrityError), write_tx(conn):
        conn.execute("INSERT INTO categories (name) VALUES ('tools')")


def test_one_open_proposal_per_item(conn, inbox_id):
    item_id = add_item(conn, inbox_id)
    insert = (
        "INSERT INTO review_proposals (type, item_id, status) VALUES ('missing', ?, 'open') "
        "ON CONFLICT(item_id) WHERE status = 'open' DO NOTHING"
    )
    with write_tx(conn):
        conn.execute(insert, (item_id,))
        conn.execute(insert, (item_id,))
    assert conn.execute("SELECT count(*) FROM review_proposals").fetchone()[0] == 1
    with write_tx(conn):
        conn.execute("UPDATE review_proposals SET status = 'dismissed'")
        conn.execute(insert, (item_id,))
    assert conn.execute("SELECT count(*) FROM review_proposals").fetchone()[0] == 2


@pytest.mark.parametrize(
    ("table", "sql"),
    [
        ("captures", "INSERT INTO captures (type, not_before) VALUES ('video', 'x')"),
        ("captures", "INSERT INTO captures (type, status, not_before) VALUES ('text', 'x', 'x')"),
        ("ai_calls", "INSERT INTO ai_calls (started_at, outcome) VALUES ('x', 'meh')"),
        ("review_proposals", "INSERT INTO review_proposals (type, item_id) VALUES ('odd', 1)"),
    ],
)
def test_enum_checks(conn, table, sql):
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"), write_tx(conn):
        conn.execute(sql)

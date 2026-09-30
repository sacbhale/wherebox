import sqlite3

import pytest

from app.db.connection import PRAGMAS, connect, write_tx


def count(conn):
    return conn.execute("SELECT count(*) FROM categories").fetchone()[0]


def test_commit_is_visible_to_other_connections(conn, settings):
    with write_tx(conn):
        conn.execute("INSERT INTO categories (name) VALUES ('Tools')")
    assert not conn.in_transaction
    other = connect(settings.db_path)
    try:
        assert count(other) == 1
    finally:
        other.close()


def test_rollback_on_exception(conn):
    with pytest.raises(RuntimeError, match="boom"), write_tx(conn):
        conn.execute("INSERT INTO categories (name) VALUES ('Tools')")
        raise RuntimeError("boom")
    assert not conn.in_transaction
    assert count(conn) == 0


def test_no_implicit_transaction_outside_write_tx(conn):
    conn.execute("SELECT 1")
    assert not conn.in_transaction


def test_begin_immediate_takes_write_lock_up_front(conn, settings):
    other = connect(settings.db_path)
    other.execute("PRAGMA busy_timeout = 0")
    try:
        with write_tx(conn):
            # No write has been made yet, but the write lock is already held.
            assert conn.in_transaction
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
            # WAL: readers are not blocked by the writer.
            assert count(other) == 0
            conn.execute("INSERT INTO categories (name) VALUES ('Tools')")
            assert count(other) == 0  # uncommitted work is invisible
        with write_tx(other):
            other.execute("INSERT INTO categories (name) VALUES ('Garden')")
        assert count(conn) == 2
    finally:
        other.close()


def test_busy_timeout_is_set_before_journal_mode():
    pragmas = list(PRAGMAS)
    assert pragmas.index("PRAGMA busy_timeout=5000") < pragmas.index("PRAGMA journal_mode=WAL")

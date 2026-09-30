"""Categories: case-insensitive unique names; rename and merge reindex affected items."""

import sqlite3

from app.db.connection import write_tx
from app.db.timestamps import NOW
from app.inventory import search


class CategoryError(ValueError):
    """A rejected category change (HTTP 400)."""


def _clean(name: str) -> str:
    return " ".join(name.split())


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM categories ORDER BY name COLLATE NOCASE").fetchall()


def get_or_create(conn: sqlite3.Connection, name: str | None) -> int | None:
    """Category id for ``name`` (matched case-insensitively), creating it if needed.
    Blank -> None. Call inside write_tx()."""
    name = _clean(name or "")
    if not name:
        return None
    row = conn.execute("SELECT id FROM categories WHERE name = ?", (name,)).fetchone()
    if row:
        return row[0]
    return conn.execute("INSERT INTO categories (name) VALUES (?)", (name,)).lastrowid


def _item_ids(conn: sqlite3.Connection, category_ids: tuple[int, ...]) -> list[int]:
    marks = ",".join("?" * len(category_ids))
    return [
        row[0]
        for row in conn.execute(
            f"SELECT id FROM items WHERE category_id IN ({marks}) AND deleted_at IS NULL",
            category_ids,
        ).fetchall()
    ]


def rename(conn: sqlite3.Connection, category_id: int, name: str) -> None:
    name = _clean(name)
    if not name:
        raise CategoryError("Name is required.")
    with write_tx(conn):
        clash = conn.execute(
            "SELECT id FROM categories WHERE name = ? AND id != ?", (name, category_id)
        ).fetchone()
        if clash:
            raise CategoryError("Another category has that name; merge them instead.")
        cur = conn.execute("UPDATE categories SET name = ? WHERE id = ?", (name, category_id))
        if cur.rowcount == 0:
            raise CategoryError("Category not found.")
        search.reindex_items(conn, _item_ids(conn, (category_id,)))


def merge(conn: sqlite3.Connection, source_id: int, target_id: int) -> None:
    """Move every item of ``source_id`` to ``target_id`` and remove the source category."""
    if source_id == target_id:
        raise CategoryError("Cannot merge a category into itself.")
    with write_tx(conn):
        found = conn.execute(
            "SELECT count(*) FROM categories WHERE id IN (?, ?)", (source_id, target_id)
        ).fetchone()[0]
        if found != 2:
            raise CategoryError("Category not found.")
        affected = _item_ids(conn, (source_id,))
        conn.execute(
            f"UPDATE items SET category_id = ?, updated_at = {NOW} WHERE category_id = ?",
            (target_id, source_id),
        )
        conn.execute("DELETE FROM categories WHERE id = ?", (source_id,))
        search.reindex_items(conn, affected)

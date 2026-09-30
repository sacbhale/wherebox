"""Shared helpers for inventory tests (import as ``from tests.inventory_helpers import ...``)."""

import sqlite3
from contextlib import closing

from app.config import Settings
from app.db.connection import connect, write_tx
from app.inventory import items, locations, search

HX = {"HX-Request": "true"}


def open_db(settings: Settings) -> closing[sqlite3.Connection]:
    return closing(connect(settings.db_path))


def make_chain(conn: sqlite3.Connection, names: list[str]) -> list[int]:
    """Create nested locations names[0] > names[1] > ...; returns their ids."""
    ids: list[int] = []
    parent = None
    for name in names:
        parent, _ = locations.create(conn, name, parent)
        ids.append(parent)
    return ids


def item_location(settings: Settings, item_id: int) -> int:
    with open_db(settings) as conn:
        return items.get(conn, item_id)["location_id"]


def code_of(conn: sqlite3.Connection, location_id: int) -> str:
    return conn.execute("SELECT short_code FROM locations WHERE id = ?", (location_id,)).fetchone()[
        0
    ]


def seed_items(conn: sqlite3.Connection, rows: list[items.ItemData]) -> list[int]:
    """Bulk insert in one write transaction with a single reindex (like capture apply)."""
    with write_tx(conn):
        ids = [items.insert(conn, data) for data in rows]
        search.reindex_items(conn, ids)
    return ids


def fts_rows(conn: sqlite3.Connection) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT rowid, name, category, notes, location_path FROM items_fts ORDER BY rowid"
        )
    ]


def expected_fts_rows(conn: sqlite3.Connection) -> list[tuple]:
    """items_fts recomputed from scratch in Python (independent of reindex_items' SQL)."""
    locs = {row["id"]: row for row in conn.execute("SELECT * FROM locations")}

    def path(location_id: int) -> str:
        names = []
        current: int | None = location_id
        while current is not None:
            names.append(locs[current]["name"])
            current = locs[current]["parent_id"]
        return search.PATH_SEP.join(reversed(names))

    cats = {row["id"]: row["name"] for row in conn.execute("SELECT * FROM categories")}
    return [
        (
            row["id"],
            row["name"],
            cats.get(row["category_id"], ""),
            row["notes"] or "",
            path(row["location_id"]),
        )
        for row in conn.execute("SELECT * FROM items WHERE deleted_at IS NULL ORDER BY id")
    ]

"""Full-text search over items (FTS5) and the combinable browse filters.

``items_fts`` stores its own content (rowid = items.id) and has no triggers: every write that
changes an item's name, notes, category or location path calls ``reindex_items`` inside the
same ``write_tx``.
"""

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

PATH_SEP = " › "

# Every live location's full path, computed once per statement.
PATHS_CTE = f"""
paths(id, path) AS (
    SELECT id, name FROM locations WHERE parent_id IS NULL AND deleted_at IS NULL
    UNION ALL
    SELECT l.id, p.path || '{PATH_SEP}' || l.name
    FROM locations l JOIN paths p ON l.parent_id = p.id
    WHERE l.deleted_at IS NULL
)"""

_REINDEX_INSERT = f"""
INSERT INTO items_fts (rowid, name, category, notes, location_path)
WITH RECURSIVE {PATHS_CTE}
SELECT i.id, i.name, coalesce(c.name, ''), coalesce(i.notes, ''), coalesce(p.path, '')
FROM temp.reindex_ids r
JOIN items i ON i.id = r.id
LEFT JOIN categories c ON c.id = i.category_id
LEFT JOIN paths p ON p.id = i.location_id
WHERE i.deleted_at IS NULL
"""


def reindex_items(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """Rebuild the FTS rows of ``ids``: one DELETE and one INSERT ... SELECT.

    Call inside write_tx(). Soft-deleted items end up with no FTS row.
    """
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS reindex_ids (id INTEGER PRIMARY KEY)")
    conn.execute("DELETE FROM temp.reindex_ids")
    conn.executemany("INSERT OR IGNORE INTO temp.reindex_ids (id) VALUES (?)", ((i,) for i in ids))
    conn.execute("DELETE FROM items_fts WHERE rowid IN (SELECT id FROM temp.reindex_ids)")
    conn.execute(_REINDEX_INSERT)
    conn.execute("DELETE FROM temp.reindex_ids")


def remove_items(conn: sqlite3.Connection, ids: Iterable[int]) -> None:
    """Drop FTS rows (item soft delete). Call inside write_tx()."""
    conn.executemany("DELETE FROM items_fts WHERE rowid = ?", ((i,) for i in ids))


def build_fts_query(q: str) -> str | None:
    """User text -> FTS5 query that can never be a syntax error.

    Each whitespace token becomes a quoted prefix phrase with ``"`` doubled (``"batt"*``);
    tokens are joined with spaces (implicit AND). Returns None for blank input.
    """
    tokens = q.split()
    if not tokens:
        return None
    return " ".join('"' + token.replace('"', '""') + '"*' for token in tokens)


KINDS = ("consumable", "belonging")


@dataclass(frozen=True)
class Filters:
    loc: int | None = None  # location id
    sub: bool = False  # include the location's whole subtree
    category: int | None = None  # category id
    kind: str | None = None
    review: bool = False  # needs_review only
    expiring_days: int | None = None  # expiry_date <= today + N

    @property
    def active(self) -> bool:
        return any(
            (
                self.loc is not None,
                self.category is not None,
                self.kind,
                self.review,
                self.expiring_days is not None,
            )
        )


def search(
    conn: sqlite3.Connection,
    q: str,
    filters: Filters | None = None,
    limit: int = 50,
    today: date | None = None,
) -> list[sqlite3.Row]:
    """Live items matching ``q`` (may be blank: browse) and every given filter.

    Rows carry the item columns plus ``location_path``, ``location_code`` and
    ``category_name``. Ordered by FTS rank when there is a query, else by name.
    """
    filters = filters or Filters()
    fts_query = build_fts_query(q)
    ctes: list[str] = []
    where = ["i.deleted_at IS NULL"]
    params: dict[str, Any] = {"limit": limit}
    if fts_query is not None:
        where.append("items_fts MATCH :fts")
        params["fts"] = fts_query
    if filters.loc is not None:
        params["loc"] = filters.loc
        if filters.sub:
            ctes.append(
                "sub(id) AS (SELECT :loc UNION ALL SELECT l.id FROM locations l "
                "JOIN sub ON l.parent_id = sub.id WHERE l.deleted_at IS NULL)"
            )
            where.append("i.location_id IN (SELECT id FROM sub)")
        else:
            where.append("i.location_id = :loc")
    if filters.category is not None:
        where.append("i.category_id = :category")
        params["category"] = filters.category
    if filters.kind:
        where.append("i.kind = :kind")
        params["kind"] = filters.kind
    if filters.review:
        where.append("i.needs_review = 1")
    if filters.expiring_days is not None:
        where.append("i.expiry_date IS NOT NULL AND i.expiry_date <= :expiry_limit")
        params["expiry_limit"] = (
            (today or date.today()) + timedelta(days=filters.expiring_days)
        ).isoformat()
    order = "items_fts.rank, i.name" if fts_query is not None else "i.name COLLATE NOCASE, i.id"
    sql = (
        (f"WITH RECURSIVE {', '.join(ctes)} " if ctes else "")
        + "SELECT i.*, items_fts.location_path AS location_path, "
        "l.short_code AS location_code, c.name AS category_name "
        "FROM items_fts JOIN items i ON i.id = items_fts.rowid "
        "JOIN locations l ON l.id = i.location_id "
        "LEFT JOIN categories c ON c.id = i.category_id "
        f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT :limit"
    )
    return conn.execute(sql, params).fetchall()

"""Location tree repository: create, rename, move (cycle guard), soft delete with re-parenting.

Public write functions open their own ``write_tx`` and reindex affected items in it.
The system Inbox (``is_system = 1``) cannot be renamed, moved or deleted.
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Literal

from app.db.connection import write_tx
from app.db.timestamps import NOW
from app.inventory import search, shortcode

_SUBTREE_CTE = (
    "WITH RECURSIVE sub(id) AS (SELECT ? UNION ALL SELECT l.id FROM locations l "
    "JOIN sub ON l.parent_id = sub.id WHERE l.deleted_at IS NULL) "
)


class LocationError(ValueError):
    """A rejected location change (HTTP 400); the tree is left unchanged."""


@dataclass(frozen=True)
class BulkResult:
    """``create_many``: the new location ids in list order, and the names it skipped, each
    with the id of the live sibling it matched (a new id when the name was listed twice)."""

    created: list[int]
    skipped: list[tuple[str, int]]


@dataclass(frozen=True)
class DeleteCounts:
    child_locations: int
    direct_items: int


def get(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row | None:
    """A live location."""
    return conn.execute(
        "SELECT * FROM locations WHERE id = ? AND deleted_at IS NULL", (location_id,)
    ).fetchone()


def lookup_code(
    conn: sqlite3.Connection, raw_code: str
) -> tuple[Literal["live", "deleted", "unknown"], sqlite3.Row | None]:
    """Resolve a (possibly mistyped) code: normalized, then live / deleted / unknown."""
    row = conn.execute(
        "SELECT * FROM locations WHERE short_code = ?", (shortcode.normalize(raw_code),)
    ).fetchone()
    if row is None:
        return "unknown", None
    return ("deleted" if row["deleted_at"] else "live"), row


def inbox(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM locations WHERE is_system = 1").fetchone()


def children(conn: sqlite3.Connection, location_id: int | None) -> list[sqlite3.Row]:
    """Live child locations (roots when ``location_id`` is None), with live item counts."""
    return conn.execute(
        "SELECT l.*, (SELECT count(*) FROM items i WHERE i.location_id = l.id "
        "AND i.deleted_at IS NULL) AS item_count "
        "FROM locations l WHERE l.parent_id IS ? AND l.deleted_at IS NULL "
        "ORDER BY l.is_system DESC, l.name COLLATE NOCASE",
        (location_id,),
    ).fetchall()


# The home tree nests at most this deep; deeper sub-locations are reached from the deepest
# node's own page. Bounds the recursive template, which would otherwise hit Python's recursion
# limit (and 500 the home page) on a chain a few hundred levels deep.
TREE_MAX_DEPTH = 12


@dataclass
class TreeNode:
    """A live location in the home page tree, with its live direct item count."""

    id: int
    short_code: str
    name: str
    is_system: bool
    item_count: int
    sub_count: int  # live direct sub-locations, including any cut off by TREE_MAX_DEPTH
    children: list["TreeNode"] = field(default_factory=list)


def tree(conn: sqlite3.Connection) -> list[TreeNode]:
    """Every live location as nested nodes (roots first), in one query. Siblings are ordered
    like ``children``: the Inbox first, then by name.

    Built by walking down from the roots, so rows that can't reach a root (a parent cycle from
    manual DB edits) are left out rather than looping; a self-parented row counts as a root."""
    rows = conn.execute(
        "SELECT l.id, l.parent_id, l.short_code, l.name, l.is_system, "
        "count(i.id) AS item_count FROM locations l "
        "LEFT JOIN items i ON i.location_id = l.id AND i.deleted_at IS NULL "
        "WHERE l.deleted_at IS NULL GROUP BY l.id "
        "ORDER BY l.is_system DESC, l.name COLLATE NOCASE"
    ).fetchall()
    live = {row["id"] for row in rows}
    kids: dict[int | None, list[sqlite3.Row]] = {}
    for row in rows:
        parent_id = row["parent_id"]
        if parent_id not in live or parent_id == row["id"]:
            parent_id = None
        kids.setdefault(parent_id, []).append(row)

    def build(row: sqlite3.Row, depth: int) -> TreeNode:
        sub = kids.get(row["id"], [])
        node = TreeNode(
            row["id"],
            row["short_code"],
            row["name"],
            bool(row["is_system"]),
            row["item_count"],
            len(sub),
        )
        if depth < TREE_MAX_DEPTH:
            node.children = [build(child, depth + 1) for child in sub]
        return node

    return [build(row, 1) for row in kids.get(None, [])]


def ancestors(conn: sqlite3.Connection, location_id: int) -> list[sqlite3.Row]:
    """The breadcrumb: root first, the location itself last."""
    return conn.execute(
        "WITH RECURSIVE up(id, parent_id, depth) AS ("
        "SELECT id, parent_id, 0 FROM locations WHERE id = ? "
        "UNION ALL SELECT l.id, l.parent_id, up.depth + 1 FROM locations l "
        "JOIN up ON l.id = up.parent_id) "
        "SELECT l.* FROM up JOIN locations l ON l.id = up.id ORDER BY up.depth DESC",
        (location_id,),
    ).fetchall()


def path(conn: sqlite3.Connection, location_id: int) -> str:
    return search.PATH_SEP.join(row["name"] for row in ancestors(conn, location_id))


def subtree_ids(conn: sqlite3.Connection, location_id: int) -> list[int]:
    """The location and all its live descendants."""
    return [
        row[0]
        for row in conn.execute(_SUBTREE_CTE + "SELECT id FROM sub", (location_id,)).fetchall()
    ]


def all_with_paths(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every live location with its full path, ordered by path (for pickers)."""
    return conn.execute(
        f"WITH RECURSIVE {search.PATHS_CTE} "
        "SELECT l.id, l.short_code, l.name, l.is_system, p.path FROM paths p "
        "JOIN locations l ON l.id = p.id ORDER BY l.is_system DESC, p.path COLLATE NOCASE"
    ).fetchall()


def recent(conn: sqlite3.Connection, limit: int = 5) -> list[sqlite3.Row]:
    """Most recently used live locations (items created in or moved into them)."""
    return conn.execute(
        "SELECT * FROM locations WHERE deleted_at IS NULL AND last_used_at IS NOT NULL "
        "ORDER BY last_used_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def move_targets(
    conn: sqlite3.Connection, location_id: int, recent_limit: int = 5
) -> list[sqlite3.Row]:
    """Quick "Move to" destinations for an item in ``location_id`` (AC-6): the most recently
    used locations, then the parent, then the siblings; deduplicated and without the location
    itself. Rows are shaped like ``all_with_paths``."""
    ids = [row["id"] for row in recent(conn, recent_limit + 1) if row["id"] != location_id]
    ids = ids[:recent_limit]
    current = get(conn, location_id)
    if current is not None:
        if current["parent_id"] is not None:
            ids.append(current["parent_id"])
        ids += [row["id"] for row in children(conn, current["parent_id"])]
    by_id = {row["id"]: row for row in all_with_paths(conn)}
    return [by_id[i] for i in dict.fromkeys(ids) if i != location_id and i in by_id]


def find_by_path(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    """Live locations whose full path contains ``query`` (case-insensitive), for pickers."""
    needle = " ".join(query.split()).casefold()
    if not needle:
        return []
    return [row for row in all_with_paths(conn) if needle in row["path"].casefold()][:limit]


def mark_used(conn: sqlite3.Connection, location_id: int) -> None:
    """Record use for the recent-locations list. Call inside write_tx()."""
    conn.execute(f"UPDATE locations SET last_used_at = {NOW} WHERE id = ?", (location_id,))


def _clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise LocationError("Name is required.")
    if len(name) > 200:
        raise LocationError("Name is too long.")
    return name


def _require_live(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row:
    row = get(conn, location_id)
    if row is None:
        raise LocationError("Location not found.")
    return row


def _require_editable(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row:
    row = _require_live(conn, location_id)
    if row["is_system"]:
        raise LocationError("The Inbox cannot be renamed, moved or deleted.")
    return row


def _subtree_item_ids(conn: sqlite3.Connection, location_id: int) -> list[int]:
    return [
        row[0]
        for row in conn.execute(
            _SUBTREE_CTE + "SELECT i.id FROM items i JOIN sub ON i.location_id = sub.id "
            "WHERE i.deleted_at IS NULL",
            (location_id,),
        ).fetchall()
    ]


def create(conn: sqlite3.Connection, name: str, parent_id: int | None = None) -> tuple[int, str]:
    """Create a location (a root when ``parent_id`` is None). Returns (id, short_code)."""
    name = _clean_name(name)
    with write_tx(conn):
        if parent_id is not None:
            _require_live(conn, parent_id)
        return shortcode.insert_with_code(
            conn,
            "INSERT INTO locations (short_code, name, parent_id) "
            "VALUES (:short_code, :name, :parent_id)",
            {"name": name, "parent_id": parent_id},
        )


BULK_MAX = 50


def create_many(
    conn: sqlite3.Connection, names: list[str], parent_id: int | None = None
) -> BulkResult:
    """Create several locations under one parent (roots when None) in one transaction, in the
    listed order, each with its own short code. Blank names are ignored; a name matching a live
    sibling or an earlier name in the list (case-insensitively) is skipped, not created. More
    than BULK_MAX names, or any name over 200 characters, raises LocationError and creates
    nothing; the error lists the offending line numbers (1-based, blank lines counted)."""
    lines = [(number, " ".join(raw.split())) for number, raw in enumerate(names, 1)]
    lines = [(number, name) for number, name in lines if name]
    if not lines:
        raise LocationError("Enter at least one name.")
    if len(lines) > BULK_MAX:
        raise LocationError(
            f"At most {BULK_MAX} locations at a time; the list has {len(lines)}. Nothing was added."
        )
    too_long = [str(number) for number, name in lines if len(name) > 200]
    if too_long:
        raise LocationError(
            f"Names must be at most 200 characters (line{'s' if len(too_long) > 1 else ''} "
            f"{', '.join(too_long)}). Nothing was added."
        )
    created: list[int] = []
    skipped: list[tuple[str, int]] = []
    with write_tx(conn):
        if parent_id is not None:
            _require_live(conn, parent_id)
        taken = {row["name"].casefold(): row["id"] for row in children(conn, parent_id)}
        for _, name in lines:
            key = name.casefold()
            if key in taken:
                skipped.append((name, taken[key]))
                continue
            taken[key], _ = shortcode.insert_with_code(
                conn,
                "INSERT INTO locations (short_code, name, parent_id) "
                "VALUES (:short_code, :name, :parent_id)",
                {"name": name, "parent_id": parent_id},
            )
            created.append(taken[key])
    return BulkResult(created, skipped)


def rename(conn: sqlite3.Connection, location_id: int, name: str) -> None:
    name = _clean_name(name)
    with write_tx(conn):
        _require_editable(conn, location_id)
        conn.execute(
            f"UPDATE locations SET name = ?, updated_at = {NOW} WHERE id = ?", (name, location_id)
        )
        search.reindex_items(conn, _subtree_item_ids(conn, location_id))


def move(conn: sqlite3.Connection, location_id: int, new_parent_id: int | None) -> None:
    """Re-parent a location (None: make it a root). Moving it under itself or one of its
    descendants raises LocationError and changes nothing."""
    with write_tx(conn):
        row = _require_editable(conn, location_id)
        if new_parent_id is not None:
            _require_live(conn, new_parent_id)
            if location_id in {r["id"] for r in ancestors(conn, new_parent_id)}:
                raise LocationError("A location cannot be moved inside itself.")
        if row["parent_id"] == new_parent_id:
            return
        conn.execute(
            f"UPDATE locations SET parent_id = ?, updated_at = {NOW} WHERE id = ?",
            (new_parent_id, location_id),
        )
        search.reindex_items(conn, _subtree_item_ids(conn, location_id))


def delete_counts(conn: sqlite3.Connection, location_id: int) -> DeleteCounts:
    """What deleting would re-parent: live child locations and live direct items."""
    child_locations = conn.execute(
        "SELECT count(*) FROM locations WHERE parent_id = ? AND deleted_at IS NULL",
        (location_id,),
    ).fetchone()[0]
    direct_items = conn.execute(
        "SELECT count(*) FROM items WHERE location_id = ? AND deleted_at IS NULL", (location_id,)
    ).fetchone()[0]
    return DeleteCounts(child_locations, direct_items)


def delete(conn: sqlite3.Connection, location_id: int) -> DeleteCounts:
    """Soft-delete a location. Child locations move to its parent (keeping their codes);
    its items move to its parent, or to the Inbox when it is a root."""
    with write_tx(conn):
        row = _require_editable(conn, location_id)
        counts = delete_counts(conn, location_id)
        moved_subtree_items = _subtree_item_ids(conn, location_id)
        item_target = row["parent_id"] if row["parent_id"] is not None else inbox(conn)["id"]
        conn.execute(
            f"UPDATE locations SET parent_id = ?, updated_at = {NOW} "
            "WHERE parent_id = ? AND deleted_at IS NULL",
            (row["parent_id"], location_id),
        )
        # Used-up (soft-deleted) items move too, so their undo lands in a live location.
        conn.execute(
            f"UPDATE items SET location_id = ?, updated_at = {NOW} WHERE location_id = ?",
            (item_target, location_id),
        )
        conn.execute(
            f"UPDATE locations SET deleted_at = {NOW}, updated_at = {NOW} WHERE id = ?",
            (location_id,),
        )
        search.reindex_items(conn, moved_subtree_items)
    return counts

"""Review queue repository (AC-7): what needs a decision, and the one-tap resolutions.

Four sections: failed captures, items flagged ``needs_review``, open ``missing`` proposals and
open ``quantity`` proposals (from reconcile). Each resolver runs in one ``write_tx`` and returns
False when there was nothing (left) to resolve.
"""

import sqlite3

from app.db.connection import write_tx
from app.db.timestamps import NOW
from app.inventory import search

_ITEM_COLUMNS = (
    "i.id, i.name, i.quantity, i.location_id, i.confidence, i.photo_id,"
    " l.name AS location_name, l.short_code AS location_code,"
    " coalesce(p.display_rel_path, p.rel_path) AS photo_path"
)
_ITEM_JOINS = " JOIN locations l ON l.id = i.location_id LEFT JOIN photos p ON p.id = i.photo_id"


def failed_captures(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT c.*, l.name AS location_name FROM captures c"
        " LEFT JOIN locations l ON l.id = c.location_id"
        " WHERE c.status = 'failed' ORDER BY c.created_at DESC, c.id DESC"
    ).fetchall()


def flagged_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM items i{_ITEM_JOINS}"
        " WHERE i.needs_review = 1 AND i.deleted_at IS NULL"
        " ORDER BY i.created_at DESC, i.id DESC"
    ).fetchall()


def open_proposals(conn: sqlite3.Connection, kind: str) -> list[sqlite3.Row]:
    """Open proposals of one type for live items, with the item's fields."""
    return conn.execute(
        f"SELECT r.id AS proposal_id, r.proposed_quantity, {_ITEM_COLUMNS}"
        f" FROM review_proposals r JOIN items i ON i.id = r.item_id{_ITEM_JOINS}"
        " WHERE r.status = 'open' AND r.type = ? AND i.deleted_at IS NULL"
        " ORDER BY r.created_at, r.id",
        (kind,),
    ).fetchall()


def photo(conn: sqlite3.Connection, photo_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()


def keep_item(conn: sqlite3.Connection, item_id: int) -> bool:
    """Keep: the item is fine as captured. (needs_review is not indexed: no reindex.)"""
    with write_tx(conn):
        cursor = conn.execute(
            f"UPDATE items SET needs_review = 0, updated_at = {NOW}"
            " WHERE id = ? AND needs_review = 1 AND deleted_at IS NULL",
            (item_id,),
        )
    return cursor.rowcount == 1


def _open_proposal(conn: sqlite3.Connection, proposal_id: int, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT r.* FROM review_proposals r JOIN items i ON i.id = r.item_id"
        " WHERE r.id = ? AND r.type = ? AND r.status = 'open' AND i.deleted_at IS NULL",
        (proposal_id, kind),
    ).fetchone()


def _resolve(conn: sqlite3.Connection, proposal_id: int, status: str) -> None:
    conn.execute(
        f"UPDATE review_proposals SET status = ?, resolved_at = {NOW} WHERE id = ?",
        (status, proposal_id),
    )


def confirm_missing(conn: sqlite3.Connection, proposal_id: int) -> bool:
    """Confirm removal: the item is used up (soft delete, FTS row dropped) and the proposal
    accepted."""
    with write_tx(conn):
        proposal = _open_proposal(conn, proposal_id, "missing")
        if proposal is None:
            return False
        conn.execute(
            f"UPDATE items SET deleted_at = {NOW}, deleted_reason = 'used_up',"
            f" updated_at = {NOW} WHERE id = ?",
            (proposal["item_id"],),
        )
        search.remove_items(conn, [proposal["item_id"]])
        _resolve(conn, proposal_id, "accepted")
    return True


def still_here(conn: sqlite3.Connection, proposal_id: int) -> bool:
    """Still here: dismiss the missing proposal and mark the item as seen now."""
    with write_tx(conn):
        proposal = _open_proposal(conn, proposal_id, "missing")
        if proposal is None:
            return False
        conn.execute(f"UPDATE items SET last_seen_at = {NOW} WHERE id = ?", (proposal["item_id"],))
        _resolve(conn, proposal_id, "dismissed")
    return True


def accept_quantity(conn: sqlite3.Connection, proposal_id: int) -> bool:
    """Accept N: set the counted quantity (not indexed: no reindex)."""
    with write_tx(conn):
        proposal = _open_proposal(conn, proposal_id, "quantity")
        if proposal is None or proposal["proposed_quantity"] is None:
            return False
        conn.execute(
            f"UPDATE items SET quantity = ?, updated_at = {NOW} WHERE id = ?",
            (max(proposal["proposed_quantity"], 0), proposal["item_id"]),
        )
        _resolve(conn, proposal_id, "accepted")
    return True


def dismiss_quantity(conn: sqlite3.Connection, proposal_id: int) -> bool:
    with write_tx(conn):
        if _open_proposal(conn, proposal_id, "quantity") is None:
            return False
        _resolve(conn, proposal_id, "dismissed")
    return True

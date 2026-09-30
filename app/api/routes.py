"""Authenticated JSON API (access control is enforced by the auth middleware)."""

from typing import Any

from fastapi import APIRouter, HTTPException

from app.db.deps import Conn
from app.inventory.export import build_export

router = APIRouter(prefix="/api")


@router.get("/items/{item_id}")
def get_item(item_id: int, conn: Conn) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM items WHERE id = ? AND deleted_at IS NULL", (item_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    return dict(row)


@router.get("/export.json")
def export_json(conn: Conn) -> dict[str, Any]:
    """The full export (same document as ``python -m app.cli export``)."""
    return build_export(conn)

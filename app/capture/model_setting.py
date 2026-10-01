"""Repository for the active-model choice (``settings`` key ``ai_model``, read by
``app.ai.model_choice``)."""

import sqlite3

from app.ai.model_choice import SETTING_KEY
from app.ai.openrouter_catalog import valid_model_id
from app.db.connection import write_tx


def set_model(conn: sqlite3.Connection, model_id: str) -> None:
    """Store ``model_id`` as the active model (the next capture uses it). Callers validate it
    against the catalog; this only rejects ids that are not OpenRouter-shaped."""
    if not valid_model_id(model_id):
        raise ValueError(f"invalid model id {model_id!r}")
    with write_tx(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (SETTING_KEY, model_id),
        )


def clear_model(conn: sqlite3.Connection) -> None:
    """Forget the stored choice: AI_MODEL applies again."""
    with write_tx(conn):
        conn.execute("DELETE FROM settings WHERE key = ?", (SETTING_KEY,))

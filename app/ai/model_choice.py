"""The active AI model (read-only; writes live in ``app.capture.model_setting``).

The Settings page can store a model id under the ``settings`` key ``ai_model``; it overrides
AI_MODEL while the model is selectable (AI_PROVIDER=openrouter, or the test-only fake with
OPENROUTER_CATALOG_FILE). Clearing it falls back to AI_MODEL.
"""

import sqlite3

from app.config import Settings

SETTING_KEY = "ai_model"


def model_selectable(settings: Settings) -> bool:
    """True when the Settings page offers the model picker."""
    return settings.ai_provider == "openrouter" or (
        settings.ai_provider == "fake" and settings.openrouter_catalog_file is not None
    )


def stored_model(conn: sqlite3.Connection) -> str | None:
    """The model chosen on the Settings page, or None."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTING_KEY,)).fetchone()
    return row[0] if row and row[0] else None


def effective_model(conn: sqlite3.Connection, settings: Settings) -> str:
    """The model the next capture uses: the stored choice when selectable, else AI_MODEL."""
    if model_selectable(settings):
        return stored_model(conn) or settings.ai_model
    return settings.ai_model

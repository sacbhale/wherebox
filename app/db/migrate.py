"""Apply ordered *.sql migrations, then idempotent Python post-migration hooks."""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

from app.db.connection import write_tx
from app.inventory import shortcode

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _statements(sql: str) -> Iterator[str]:
    """Split a script into complete statements (trigger bodies contain ';')."""
    buf = ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            yield buf.strip()
            buf = ""
    rest = "".join(ln for ln in buf.splitlines() if not ln.strip().startswith("--")).strip()
    if rest:
        raise ValueError(f"incomplete SQL statement at end of migration: {rest[:80]!r}")


def ensure_inbox(conn: sqlite3.Connection) -> None:
    """Create the single system Inbox location if it does not exist."""
    if conn.execute("SELECT 1 FROM locations WHERE is_system = 1").fetchone():
        return
    shortcode.insert_with_code(
        conn,
        "INSERT INTO locations (short_code, name, is_system) VALUES (:short_code, 'Inbox', 1)",
        {},
    )


POST_MIGRATION_HOOKS: list[Callable[[sqlite3.Connection], None]] = [ensure_inbox]


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Bring the database up to date. Idempotent; returns the versions applied now."""
    with write_tx(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version TEXT PRIMARY KEY, "
            "applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
        )
    applied: list[str] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        with write_tx(conn):
            if conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (path.stem,)
            ).fetchone():
                continue
            for statement in _statements(path.read_text(encoding="utf-8")):
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (path.stem,))
        applied.append(path.stem)
    for hook in POST_MIGRATION_HOOKS:
        with write_tx(conn):
            hook(conn)
    return applied

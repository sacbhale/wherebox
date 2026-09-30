"""Household password: an argon2id hash in the ``settings`` table.

Every password change also deletes all sessions, in the same transaction.
"""

import sqlite3

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError

from app.db.connection import write_tx

_hasher = PasswordHasher(type=Type.ID)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def get_password_hash(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'password_hash'").fetchone()
    return row["value"] if row and row["value"] else None


def has_password(conn: sqlite3.Connection) -> bool:
    return get_password_hash(conn) is not None


def verify_hash(stored_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def verify_password(conn: sqlite3.Connection, password: str) -> bool:
    stored = get_password_hash(conn)
    return stored is not None and verify_hash(stored, password)


def set_password(conn: sqlite3.Connection, password: str) -> None:
    """Store a new hash and invalidate every session (setup, env override, CLI)."""
    if not password:
        raise ValueError("password must not be empty")
    new_hash = hash_password(password)
    with write_tx(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('password_hash', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (new_hash,),
        )
        conn.execute("DELETE FROM sessions")


def apply_env_override(conn: sqlite3.Connection, password: str) -> bool:
    """HOUSEHOLD_PASSWORD: replace the stored hash unless it verifies. True if it changed."""
    stored = get_password_hash(conn)
    if stored is not None and verify_hash(stored, password):
        return False
    set_password(conn, password)
    return True

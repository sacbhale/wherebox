"""Household login: a login name and an argon2id password hash in the ``settings`` table.

Every password or login-name change also deletes all sessions, in the same transaction.
Login names are stored as entered (trimmed) and compared case-insensitively (NFKC + casefold).
"""

import hmac
import sqlite3
import unicodedata

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


def verify_login(conn: sqlite3.Connection, username: str, password: str) -> bool:
    """Name and password must both match. The password hash is always verified, so a wrong
    name costs the same time. With no stored name (legacy mode) the password alone logs in."""
    password_ok = verify_password(conn, password)
    stored_name = get_username(conn)
    if stored_name is None:
        return password_ok
    name_ok = hmac.compare_digest(
        normalize_username(username).encode(), normalize_username(stored_name).encode()
    )
    return password_ok and name_ok


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


def set_credentials(conn: sqlite3.Connection, name: str, password: str) -> bool:
    """First-run setup: name, password and session wipe in one transaction. False (nothing
    written) if a password already exists, e.g. a concurrent setup won."""
    name = validate_username(name)
    if not password:
        raise ValueError("password must not be empty")
    new_hash = hash_password(password)
    with write_tx(conn):
        if get_password_hash(conn) is not None:
            return False
        for key, value in (("username", name), ("password_hash", new_hash)):
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        conn.execute("DELETE FROM sessions")
    return True


def apply_env_override(conn: sqlite3.Connection, password: str) -> bool:
    """HOUSEHOLD_PASSWORD: replace the stored hash unless it verifies. True if it changed."""
    stored = get_password_hash(conn)
    if stored is not None and verify_hash(stored, password):
        return False
    set_password(conn, password)
    return True


USERNAME_RULE = (
    "the login name must be 1-64 characters (after trimming) "
    "with no control or invisible characters"
)
_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def normalize_username(name: str) -> str:
    """The comparison form: trimmed, NFKC_Casefold-style, so normalizing twice changes nothing."""
    nfkc = unicodedata.normalize("NFKC", name)
    return unicodedata.normalize("NFKC", nfkc.casefold()).strip()  # NFKC can yield spaces


def validate_username(name: str) -> str:
    """The name to store (trimmed); ValueError naming the rule when invalid."""
    name = name.strip()
    if not 1 <= len(name) <= 64 or any(
        unicodedata.category(ch) in _FORBIDDEN_CATEGORIES for ch in name
    ):
        raise ValueError(USERNAME_RULE)
    return name


def get_username(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'username'").fetchone()
    return row["value"] if row and row["value"] else None


def set_username(conn: sqlite3.Connection, name: str) -> None:
    """Store a new login name and invalidate every session (setup, env override, CLI)."""
    name = validate_username(name)
    with write_tx(conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('username', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (name,),
        )
        conn.execute("DELETE FROM sessions")


def apply_username_env_override(conn: sqlite3.Connection, name: str) -> bool:
    """HOUSEHOLD_USERNAME: store it unless it matches the stored name. True if it changed."""
    stored = get_username(conn)
    if stored is not None and normalize_username(stored) == normalize_username(name):
        return False
    set_username(conn, name)
    return True

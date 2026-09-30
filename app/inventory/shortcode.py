"""Permanent location short codes: 6 Crockford base32 characters."""

import secrets
import sqlite3
from collections.abc import Mapping
from typing import Any

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32: no I, L, O, U
LENGTH = 6
MAX_ATTEMPTS = 5

_CONFUSABLES = str.maketrans({"O": "0", "I": "1", "L": "1", "-": None})


def generate() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def normalize(code: str) -> str:
    """Canonical form of a user-typed or scanned code: uppercase, O->0, I/L->1, no dashes."""
    return code.strip().upper().translate(_CONFUSABLES)


def insert_with_code(
    conn: sqlite3.Connection, sql: str, params: Mapping[str, Any]
) -> tuple[int, str]:
    """Run an INSERT whose ``:short_code`` parameter gets a fresh code.

    Retries with a new code up to MAX_ATTEMPTS times on a UNIQUE conflict on short_code.
    Call inside write_tx(); a failed INSERT only aborts its own statement, not the transaction.
    Returns (rowid, code).
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        code = generate()
        try:
            cur = conn.execute(sql, {**params, "short_code": code})
        except sqlite3.IntegrityError as exc:
            msg = str(exc)
            if "UNIQUE" in msg and "short_code" in msg and attempt < MAX_ATTEMPTS:
                continue
            raise
        return cur.lastrowid, code
    raise AssertionError("unreachable")

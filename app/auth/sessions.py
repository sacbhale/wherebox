"""Server-side sessions: the ``inv_session`` cookie holds 32 random bytes; the DB stores sha256.

Time comes from ``app.state.clock`` (``timestamps.utcnow`` by default) so tests can move it.

Scan-to-move: ``sessions.pending_move`` holds ``{"item_ids": [...], "expires_at": ...}`` for
10 minutes. The next ``GET /l/<code>`` of a live location in the same session applies it.
A pending move survives its session's expiry: the expired row is kept (it no longer
authenticates) until the pending move expires, and logging in again from the same browser
(which still sends the old cookie) carries the pending move over to the new session. It lives
in the browser's session, so a scan that opens a *different* browser (for example the phone's
default browser while the app runs as a home-screen web app) does not see it.
"""

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request, Response

from app.config import Settings
from app.db.connection import write_tx

COOKIE_NAME = "inv_session"
REFRESH_BELOW = timedelta(days=15)
PENDING_MOVE_TTL = timedelta(minutes=10)


def now(request: Request) -> datetime:
    return request.app.state.clock()


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class Session:
    token_hash: str
    expires_at: datetime
    refreshed: bool = False


@dataclass(frozen=True)
class PendingMove:
    item_ids: tuple[int, ...]
    expires_at: datetime


def _live_pending(value: str | None, at: datetime) -> PendingMove | None:
    """The stored pending move if it is well-formed and not expired at ``at``."""
    if not value:
        return None
    try:
        data = json.loads(value)
        move = PendingMove(
            tuple(int(item_id) for item_id in data["item_ids"]),
            datetime.fromisoformat(data["expires_at"]),
        )
    except (ValueError, TypeError, KeyError):
        return None
    return move if move.item_ids and move.expires_at > at else None


def create_session(
    conn: sqlite3.Connection, at: datetime, days: int, previous_token: str | None = None
) -> str:
    """Create a session. ``previous_token`` is the cookie the browser still sent (usually an
    expired session): that row is replaced, and its unexpired pending move carries over."""
    token = secrets.token_urlsafe(32)
    with write_tx(conn):
        pending = None
        if previous_token:
            old = conn.execute(
                "DELETE FROM sessions WHERE token_hash = ? RETURNING pending_move",
                (_hash(previous_token),),
            ).fetchone()
            if old is not None and _live_pending(old[0], at) is not None:
                pending = old[0]
        conn.execute(
            "INSERT INTO sessions (token_hash, created_at, expires_at, last_seen_at, "
            "pending_move) VALUES (?, ?, ?, ?, ?)",
            (_hash(token), _fmt(at), _fmt(at + timedelta(days=days)), _fmt(at), pending),
        )
    return token


def load_session(conn: sqlite3.Connection, token: str, at: datetime, days: int) -> Session | None:
    """Return the live session for ``token``, extending it when under 15 days remain.

    An expired session is deleted, unless it still holds an unexpired pending move (kept for
    ``create_session`` to carry over after the user logs in again)."""
    token_hash = _hash(token)
    row = conn.execute(
        "SELECT expires_at, pending_move FROM sessions WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row is None:
        return None
    expires_at = datetime.fromisoformat(row["expires_at"])
    if expires_at <= at:
        if _live_pending(row["pending_move"], at) is None:
            with write_tx(conn):
                conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
        return None
    if expires_at - at >= REFRESH_BELOW:
        return Session(token_hash, expires_at)
    expires_at = at + timedelta(days=days)
    with write_tx(conn):
        conn.execute(
            "UPDATE sessions SET expires_at = ?, last_seen_at = ? WHERE token_hash = ?",
            (_fmt(expires_at), _fmt(at), token_hash),
        )
    return Session(token_hash, expires_at, refreshed=True)


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    with write_tx(conn):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


def set_pending_move(
    conn: sqlite3.Connection, token_hash: str, item_ids: list[int], at: datetime
) -> PendingMove:
    """Remember items to move to the next scanned location (replaces any earlier one)."""
    move = PendingMove(tuple(item_ids), at + PENDING_MOVE_TTL)
    value = json.dumps({"item_ids": list(move.item_ids), "expires_at": _fmt(move.expires_at)})
    with write_tx(conn):
        conn.execute(
            "UPDATE sessions SET pending_move = ? WHERE token_hash = ?", (value, token_hash)
        )
    return move


def get_pending_move(conn: sqlite3.Connection, token_hash: str, at: datetime) -> PendingMove | None:
    """The session's unexpired pending move; an expired or malformed one is cleared."""
    row = conn.execute(
        "SELECT pending_move FROM sessions WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if row is None or row[0] is None:
        return None
    move = _live_pending(row[0], at)
    if move is None:
        clear_pending_move(conn, token_hash)
    return move


def take_pending_move(
    conn: sqlite3.Connection, token_hash: str, at: datetime
) -> PendingMove | None:
    """Read and clear the pending move in one transaction (so only one request applies it).
    Returns None when there is none or it expired."""
    with write_tx(conn):
        row = conn.execute(
            "SELECT pending_move FROM sessions WHERE token_hash = ?", (token_hash,)
        ).fetchone()
        if row is None or row[0] is None:
            return None
        conn.execute("UPDATE sessions SET pending_move = NULL WHERE token_hash = ?", (token_hash,))
    return _live_pending(row[0], at)


def clear_pending_move(conn: sqlite3.Connection, token_hash: str) -> None:
    with write_tx(conn):
        conn.execute("UPDATE sessions SET pending_move = NULL WHERE token_hash = ?", (token_hash,))


def cookie_secure(settings: Settings, request: Request) -> bool:
    """auto: follow the effective scheme after uvicorn's trusted-proxy handling."""
    if settings.cookie_secure == "auto":
        return request.url.scheme == "https"
    return settings.cookie_secure == "true"


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    settings: Settings = request.app.state.settings
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=settings.session_days * 86400,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_secure(settings, request),
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        samesite="lax",
        secure=cookie_secure(request.app.state.settings, request),
    )

"""Login, logout and first-run setup pages."""

import logging
import secrets
import sqlite3
from typing import Annotated

from fastapi import APIRouter, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from app.auth import passwords, sessions
from app.auth.throttle import LoginThrottle
from app.config import Settings
from app.db.deps import Conn
from app.templating import templates

logger = logging.getLogger("app.auth")

router = APIRouter()


def safe_next(value: str | None) -> str:
    """Only same-site relative paths: must start with '/' and not '//' (or '/\\')."""
    if (
        not value
        or not value.startswith("/")
        or value.startswith(("//", "/\\"))
        or any(ord(ch) < 0x20 or ch == "\x7f" for ch in value)
    ):
        return "/"
    return value


def ensure_setup_token(request: Request) -> str:
    """The one-time setup token: generated when no password exists, printed to the logs."""
    state = request.app.state
    if state.setup_token is None:
        state.setup_token = new_setup_token()
    return state.setup_token


def new_setup_token() -> str:
    token = secrets.token_urlsafe(32)
    logger.warning(
        "\n%s\nNo household password is set. Open /setup and enter this one-time "
        "setup token:\n\n    SETUP TOKEN: %s\n\n%s",
        "=" * 72,
        token,
        "=" * 72,
    )
    return token


def init_auth(app: FastAPI, conn: sqlite3.Connection) -> None:
    """Startup: apply HOUSEHOLD_USERNAME and HOUSEHOLD_PASSWORD, then issue a setup token if
    no password exists."""
    settings: Settings = app.state.settings
    if settings.household_username is not None and passwords.apply_username_env_override(
        conn, settings.household_username
    ):
        logger.warning("HOUSEHOLD_USERNAME applied: login name set, sessions cleared")
    if settings.household_password is not None:
        password = settings.household_password.get_secret_value()
        if password and passwords.apply_env_override(conn, password):
            logger.warning("HOUSEHOLD_PASSWORD applied: stored password replaced, sessions cleared")
    app.state.password_set = passwords.has_password(conn)
    app.state.setup_token = None if app.state.password_set else new_setup_token()


def _login_page(request: Request, next_path: str, error: str | None, status: int) -> Response:
    return templates.TemplateResponse(
        request, "login.html", {"next": next_path, "error": error}, status_code=status
    )


def _too_many(request: Request, throttle: LoginThrottle, ip: str, target: str) -> Response | None:
    retry_after = throttle.retry_after(ip)
    if retry_after is None:
        return None
    response = _login_page(request, target, "Too many attempts. Try again later.", 429)
    response.headers["Retry-After"] = str(retry_after)
    return response


@router.get("/login")
def login_form(request: Request, conn: Conn, next: str | None = None) -> Response:
    if not passwords.has_password(conn):
        return RedirectResponse("/setup", status_code=303)
    return _login_page(request, safe_next(next), None, 200)


@router.post("/login")
async def login(
    request: Request,
    conn: Conn,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/",
) -> Response:
    target = safe_next(next)
    if not await run_in_threadpool(passwords.has_password, conn):
        return RedirectResponse("/setup", status_code=303)
    throttle: LoginThrottle = request.app.state.throttle
    ip = request.client.host if request.client else "unknown"
    blocked = _too_many(request, throttle, ip, target)
    if blocked:
        return blocked
    await throttle.wait()
    # Re-check: other attempts from this IP may have failed while this one waited.
    blocked = _too_many(request, throttle, ip, target)
    if blocked:
        return blocked
    throttle.reserve(ip)  # counts as a failure until verified, so concurrent tries can't exceed
    try:
        verified = await run_in_threadpool(passwords.verify_login, conn, username, password)
    finally:
        throttle.release(ip)
    if not verified:
        throttle.record_failure(ip)
        return _login_page(request, target, "Wrong login name or password.", 401)
    throttle.record_success(ip)
    settings = request.app.state.settings
    # The old (typically expired) cookie lets a pending scan-to-move survive re-login.
    token = await run_in_threadpool(
        sessions.create_session,
        conn,
        sessions.now(request),
        settings.session_days,
        request.cookies.get(sessions.COOKIE_NAME),
    )
    response = RedirectResponse(target, status_code=303)
    sessions.set_session_cookie(response, request, token)
    return response


@router.post("/logout")
def logout(request: Request, conn: Conn) -> Response:
    token = request.cookies.get(sessions.COOKIE_NAME)
    if token:
        sessions.delete_session(conn, token)
    response = RedirectResponse("/login", status_code=303)
    sessions.clear_session_cookie(response, request)
    return response


def _setup_page(
    request: Request, conn: sqlite3.Connection, error: str | None, status: int
) -> Response:
    locked = request.app.state.settings.household_username is not None
    name = passwords.get_username(conn) if locked else None
    return templates.TemplateResponse(
        request,
        "setup.html",
        {"error": error, "username": name or "", "locked": locked},
        status_code=status,
    )


@router.get("/setup")
def setup_form(request: Request, conn: Conn) -> Response:
    if passwords.has_password(conn):
        raise HTTPException(status_code=404)
    ensure_setup_token(request)
    return _setup_page(request, conn, None, 200)


@router.post("/setup")
def setup(
    request: Request,
    conn: Conn,
    token: Annotated[str, Form()] = "",
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    if passwords.has_password(conn):
        raise HTTPException(status_code=404)
    expected = ensure_setup_token(request)
    if not token or not secrets.compare_digest(token.encode(), expected.encode()):
        return _setup_page(request, conn, "Invalid setup token.", 403)
    if request.app.state.settings.household_username is not None:
        username = passwords.get_username(conn) or ""  # HOUSEHOLD_USERNAME wins
    error = None
    if not password:
        error = "Choose a password."
    elif password != confirm:
        error = "The passwords do not match."
    else:
        try:
            username = passwords.validate_username(username)
        except ValueError:
            error = "Choose a login name (1-64 characters, no control or invisible characters)."
    if error:
        return _setup_page(request, conn, error, 400)
    if not passwords.set_credentials(conn, username, password):
        raise HTTPException(status_code=404)
    request.app.state.setup_token = None
    request.app.state.password_set = True
    logger.info("Household login name and password set via /setup; setup token discarded")
    token_value = sessions.create_session(
        conn, sessions.now(request), request.app.state.settings.session_days
    )
    response = RedirectResponse("/", status_code=303)
    sessions.set_session_cookie(response, request, token_value)
    return response

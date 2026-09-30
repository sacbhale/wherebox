"""CSRF origin check and access control for every request.

- Unsafe methods failing the origin check -> 403 (before anything else).
- Allowlisted paths (/login, /setup, /static/*, /healthz) pass through.
- While no password is set, everything else redirects to /setup.
- Unauthenticated: /api/* -> 401 JSON; htmx -> 401 + HX-Redirect; pages -> 303 to
  ``/login?next=<path+query>``. All Location headers are relative.
- Authenticated: ``request.state.session`` holds the ``Session``.
"""

from contextlib import closing
from urllib.parse import urlencode, urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint

from app.auth import csrf, passwords, sessions
from app.config import Settings
from app.db.connection import connect

ALLOWLIST = frozenset({"/login", "/setup", "/healthz"})
ALLOWLIST_PREFIXES = ("/static/",)


def is_allowlisted(path: str) -> bool:
    return path in ALLOWLIST or path.startswith(ALLOWLIST_PREFIXES)


def login_url(next_path: str | None) -> str:
    if not next_path or next_path == "/":
        return "/login"
    return "/login?" + urlencode({"next": next_path})


def _next_for(request: Request) -> str:
    if csrf.is_htmx(request):
        # For htmx, return to the page the user is on, not the partial's URL.
        current = request.headers.get("hx-current-url")
        if not current:
            return "/"
        parts = urlsplit(current)
        return parts.path + (f"?{parts.query}" if parts.query else "")
    query = request.url.query
    return request.url.path + (f"?{query}" if query else "")


def deny(request: Request, target: str) -> Response:
    """Response for a request that may not proceed; ``target`` is a relative URL."""
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "authentication required"}, status_code=401)
    if csrf.is_htmx(request):
        return Response(status_code=401, headers={"HX-Redirect": target})
    return RedirectResponse(target, status_code=303)


def _authenticate(request: Request, token: str | None) -> tuple[bool, sessions.Session | None]:
    settings: Settings = request.app.state.settings
    with closing(connect(settings.db_path)) as conn:
        if not request.app.state.password_set:
            # Cached once seen: a password is never unset while the app runs.
            request.app.state.password_set = passwords.has_password(conn)
            if not request.app.state.password_set:
                return False, None
        if not token:
            return True, None
        return True, sessions.load_session(
            conn, token, sessions.now(request), settings.session_days
        )


async def auth_middleware(request: Request, call_next: RequestResponseEndpoint) -> Response:
    if request.method in csrf.UNSAFE_METHODS and not csrf.origin_allowed(request):
        return PlainTextResponse("Cross-origin request refused", status_code=403)
    if is_allowlisted(request.url.path):
        return await call_next(request)
    token = request.cookies.get(sessions.COOKIE_NAME)
    password_set, session = await run_in_threadpool(_authenticate, request, token)
    if not password_set:
        return deny(request, "/setup")
    if session is None or token is None:
        return deny(request, login_url(_next_for(request)))
    request.state.session = session
    response = await call_next(request)
    if session.refreshed:
        sessions.set_session_cookie(response, request, token)
    return response

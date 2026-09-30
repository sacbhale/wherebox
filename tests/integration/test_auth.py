"""Access control, login/logout, sessions, password changes and throttling.

Covers AC-10, AC-27, AC-28, AC-29, AC-30 and the relative-redirect part of AC-31.
"""

import io
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from app import cli
from app.auth import passwords
from app.auth.middleware import is_allowlisted
from app.auth.routes import safe_next
from app.auth.sessions import COOKIE_NAME
from app.config import Settings
from app.db.connection import connect, write_tx
from app.db.timestamps import utcnow
from app.main import create_app
from tests.conftest import ORIGIN, TEST_PASSWORD

# ---------------------------------------------------------------- AC-27: every route


def _protected_routes() -> list[tuple[str, str]]:
    """Every (method, path) registered on the app, minus the allowlist. Built at collection
    time from ``app.routes`` so routes added in later steps are covered automatically."""
    settings = Settings.model_construct(
        public_base_url="http://testserver", data_dir=Path("/nonexistent")
    )
    routes: set[tuple[str, str]] = set()
    for route in create_app(settings).routes:
        if isinstance(route, Mount):
            assert is_allowlisted(route.path + "/"), f"unexpected mount {route.path}"
            continue
        if not isinstance(route, APIRoute) or is_allowlisted(route.path):
            continue
        path = re.sub(r"\{[^}]+\}", "1", route.path)
        routes.update((method, path) for method in route.methods - {"HEAD"})
    return sorted(routes)


PROTECTED = _protected_routes()


def test_protected_route_list_includes_known_routes():
    assert ("GET", "/api/export.json") in PROTECTED
    assert ("GET", "/api/items/1") in PROTECTED
    assert ("GET", "/") in PROTECTED
    assert ("POST", "/logout") in PROTECTED


@pytest.fixture
def anon(auth_settings):
    """Password set, not logged in; sends the right Origin so CSRF is not what blocks."""
    with TestClient(create_app(auth_settings), headers={"Origin": ORIGIN}) as test_client:
        yield test_client


@pytest.mark.parametrize(("method", "path"), PROTECTED)
def test_unauthenticated_access_is_denied(anon, method, path):
    response = anon.request(method, path, follow_redirects=False)
    if path.startswith("/api/"):
        assert response.status_code == 401
        assert response.json() == {"detail": "authentication required"}
    else:
        assert response.status_code == 303
        location = response.headers["location"]
        assert location == "/login" or location.startswith("/login?next=")

    hx = anon.request(
        method,
        path,
        headers={"HX-Request": "true", "HX-Current-URL": "http://testserver/l/ABC123"},
        follow_redirects=False,
    )
    assert hx.status_code == 401
    if not path.startswith("/api/"):
        assert hx.headers["hx-redirect"] == "/login?next=%2Fl%2FABC123"


def test_invalid_cookie_is_unauthenticated(anon):
    anon.cookies.set(COOKIE_NAME, "forged")
    assert anon.get("/api/export.json").status_code == 401


def test_allowlist_is_open(anon):
    assert anon.get("/healthz").status_code == 200
    assert anon.get("/login").status_code == 200
    assert anon.get("/static/app.css").status_code == 200


def test_authenticated_api_routes(auth_client, settings):
    export = auth_client.get("/api/export.json")
    assert export.status_code == 200
    body = export.json()
    assert [loc["name"] for loc in body["locations"]] == ["Inbox"]
    assert body["items"] == []

    assert auth_client.get("/api/items/1").status_code == 404
    conn = connect(settings.db_path)
    try:
        with write_tx(conn):
            conn.execute(
                "INSERT INTO items (name, kind, location_id) "
                "VALUES ('AA batteries', 'consumable', ?)",
                (body["locations"][0]["id"],),
            )
    finally:
        conn.close()
    item = auth_client.get("/api/items/1").json()
    assert item["name"] == "AA batteries"
    assert auth_client.get("/api/export.json").json()["items"][0]["name"] == "AA batteries"


# ---------------------------------------------------------------- AC-10: login redirect


def test_logged_out_location_url_round_trips_through_login(anon):
    response = anon.get("/l/ABC123", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2Fl%2FABC123"

    page = anon.get(response.headers["location"])
    assert 'name="next" value="/l/ABC123"' in page.text

    login = anon.post(
        "/login", data={"password": TEST_PASSWORD, "next": "/l/ABC123"}, follow_redirects=False
    )
    assert login.status_code == 303
    assert login.headers["location"] == "/l/ABC123"


def test_next_keeps_query_string(anon):
    response = anon.get("/search?q=batt&sub=1", follow_redirects=False)
    assert response.headers["location"] == "/login?next=%2Fsearch%3Fq%3Dbatt%26sub%3D1"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/l/ABC123", "/l/ABC123"),
        ("/search?q=a", "/search?q=a"),
        ("//evil.example", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("evil", "/"),
        ("", "/"),
        (None, "/"),
        ("/a\r\nSet-Cookie: x=1", "/"),
    ],
)
def test_safe_next(value, expected):
    assert safe_next(value) == expected


def test_login_with_unsafe_next_lands_on_home(anon):
    login = anon.post(
        "/login", data={"password": TEST_PASSWORD, "next": "//evil.example"}, follow_redirects=False
    )
    assert login.headers["location"] == "/"


def test_wrong_password_is_rejected_without_cookie(anon):
    response = anon.post("/login", data={"password": "nope"}, follow_redirects=False)
    assert response.status_code == 401
    assert "set-cookie" not in response.headers
    assert "Wrong password" in response.text


def test_all_redirects_are_relative(anon, make_settings, tmp_path):
    """Every Location header in the auth flows is a relative path, even over https."""
    responses = [
        anon.get("/", follow_redirects=False),
        anon.get("/l/ABC123", follow_redirects=False),
        anon.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False),
        anon.post("/logout", follow_redirects=False),
    ]
    https = TestClient(anon.app, base_url="https://inv.example.com", headers={"Origin": ORIGIN})
    responses.append(https.get("/items?x=1", follow_redirects=False))
    fresh_settings = make_settings(data_dir=tmp_path / "fresh")  # no password yet
    with TestClient(create_app(fresh_settings), headers={"Origin": ORIGIN}) as fresh:
        responses.append(fresh.get("/", follow_redirects=False))
        responses.append(fresh.get("/login", follow_redirects=False))
        token = fresh.app.state.setup_token
        responses.append(
            fresh.post(
                "/setup",
                data={"token": token, "password": "pw", "confirm": "pw"},
                follow_redirects=False,
            )
        )
    for response in responses:
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("/") and not location.startswith("//"), location
        assert "://" not in location


# ---------------------------------------------------------------- sessions (AC-29)


def test_session_cookie_attributes(anon):
    response = anon.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE_NAME}=")
    assert "Max-Age=2592000" in cookie  # 30 days
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie  # COOKIE_SECURE=auto over plain http


def test_session_token_is_stored_hashed(auth_client, settings):
    token = auth_client.cookies[COOKIE_NAME]
    conn = connect(settings.db_path)
    try:
        hashes = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
    finally:
        conn.close()
    assert len(hashes) == 1
    assert token not in hashes[0]
    assert len(hashes[0]) == 64


@pytest.mark.parametrize(("setting", "secure"), [("true", True), ("false", False)])
def test_cookie_secure_forced(make_settings, setting, secure):
    settings = make_settings(household_password=TEST_PASSWORD, cookie_secure=setting)
    # Forced values ignore the scheme: true is Secure over http, false is not over https.
    base = "http://testserver" if setting == "true" else "https://testserver"
    with TestClient(create_app(settings), base_url=base) as client:
        response = client.post(
            "/login",
            data={"password": TEST_PASSWORD},
            headers={"Origin": base},
            follow_redirects=False,
        )
    assert ("Secure" in response.headers["set-cookie"]) is secure


def test_session_valid_on_day_29_invalid_on_day_31(auth_client):
    start = utcnow()
    auth_client.app.state.clock = lambda: start + timedelta(days=29)
    assert auth_client.get("/api/export.json").status_code == 200
    # Day 29 had 1 day left, so that request refreshed the session to day 29 + 30.
    auth_client.app.state.clock = lambda: start + timedelta(days=58)
    assert auth_client.get("/api/export.json").status_code == 200


def test_session_expires_after_30_days_without_use(auth_client):
    start = utcnow()
    auth_client.app.state.clock = lambda: start + timedelta(days=31)
    assert auth_client.get("/api/export.json").status_code == 401
    # The expired row is gone; going back in time does not revive it.
    auth_client.app.state.clock = lambda: start
    assert auth_client.get("/api/export.json").status_code == 401


def test_session_refreshed_when_under_15_days_left(auth_client, settings):
    start = utcnow()

    def expires_at() -> str:
        conn = connect(settings.db_path)
        try:
            return conn.execute("SELECT expires_at FROM sessions").fetchone()[0]
        finally:
            conn.close()

    original = expires_at()
    auth_client.app.state.clock = lambda: start + timedelta(days=10)  # 20 days left
    response = auth_client.get("/api/export.json")
    assert "set-cookie" not in response.headers
    assert expires_at() == original

    auth_client.app.state.clock = lambda: start + timedelta(days=16)  # 14 days left
    response = auth_client.get("/api/export.json")
    assert "Max-Age=2592000" in response.headers["set-cookie"]
    assert expires_at() > original


def test_logout_deletes_session(auth_client, settings):
    token = auth_client.cookies[COOKIE_NAME]
    response = auth_client.post("/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert f'{COOKIE_NAME}=""' in response.headers["set-cookie"]
    conn = connect(settings.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        conn.close()
    auth_client.cookies.set(COOKIE_NAME, token)  # replaying the old cookie does not work
    assert auth_client.get("/api/export.json").status_code == 401


# ---------------------------------------------------------------- passwords (AC-28)


def _hash(settings) -> str:
    conn = connect(settings.db_path)
    try:
        return passwords.get_password_hash(conn)
    finally:
        conn.close()


def test_env_password_stored_as_argon2id_only(auth_client, auth_settings):
    stored = _hash(auth_settings)
    assert stored.startswith("$argon2id$")
    conn = connect(auth_settings.db_path)
    try:
        values = [r[0] for r in conn.execute("SELECT value FROM settings")]
    finally:
        conn.close()
    assert all(TEST_PASSWORD not in (v or "") for v in values)


def test_password_change_invalidates_sessions(auth_client, auth_settings):
    assert auth_client.get("/api/export.json").status_code == 200
    conn = connect(auth_settings.db_path)
    try:
        passwords.set_password(conn, "a new password")
        assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    finally:
        conn.close()
    assert auth_client.get("/api/export.json").status_code == 401
    bad = auth_client.post("/login", data={"password": TEST_PASSWORD}, follow_redirects=False)
    assert bad.status_code == 401
    good = auth_client.post("/login", data={"password": "a new password"}, follow_redirects=False)
    assert good.status_code == 303


def test_env_override_replaces_changed_hash_and_clears_sessions(make_settings):
    first = make_settings(household_password=TEST_PASSWORD)
    with TestClient(create_app(first), headers={"Origin": ORIGIN}) as client:
        client.post("/login", data={"password": TEST_PASSWORD})
        cookie = client.cookies[COOKIE_NAME]
    original_hash = _hash(first)

    # Same env password on restart: hash untouched (verified, not string-compared), session kept.
    with TestClient(create_app(first), headers={"Origin": ORIGIN}) as client:
        client.cookies.set(COOKIE_NAME, cookie)
        assert client.get("/api/export.json").status_code == 200
    assert _hash(first) == original_hash

    # Changed env password on restart: replaced, all sessions cleared.
    second = make_settings(household_password="rotated password")
    with TestClient(create_app(second), headers={"Origin": ORIGIN}) as client:
        client.cookies.set(COOKIE_NAME, cookie)
        assert client.get("/api/export.json").status_code == 401
        ok = client.post("/login", data={"password": "rotated password"}, follow_redirects=False)
        assert ok.status_code == 303
    assert _hash(second) != original_hash


def test_cli_set_password_stores_hash_and_clears_sessions(auth_client, make_settings, monkeypatch):
    assert auth_client.get("/api/export.json").status_code == 200
    monkeypatch.setattr("sys.stdin", io.StringIO("from the cli\n"))
    assert cli.main(["set-password"], make_settings()) == 0
    assert auth_client.get("/api/export.json").status_code == 401
    ok = auth_client.post("/login", data={"password": "from the cli"}, follow_redirects=False)
    assert ok.status_code == 303
    assert _hash(make_settings()).startswith("$argon2id$")


def test_cli_rejects_empty_password(make_settings, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    assert cli.main(["set-password"], make_settings()) == 1


# ---------------------------------------------------------------- throttle (AC-30)


def _attempt(client: TestClient, password: str):
    return client.post("/login", data={"password": password}, follow_redirects=False)


def test_sixth_failure_from_one_ip_is_429_other_ip_unaffected(auth_settings):
    app = create_app(auth_settings)
    with TestClient(app, headers={"Origin": ORIGIN}, client=("198.51.100.1", 1)) as attacker:
        assert [_attempt(attacker, "wrong").status_code for _ in range(5)] == [401] * 5
        blocked = _attempt(attacker, "wrong")
        assert blocked.status_code == 429
        assert 0 < int(blocked.headers["retry-after"]) <= 900
        # Even the correct password is refused from the blocked IP until the window passes.
        assert _attempt(attacker, TEST_PASSWORD).status_code == 429
    with TestClient(app, headers={"Origin": ORIGIN}, client=("198.51.100.2", 1)) as other:
        assert _attempt(other, "wrong").status_code == 401
        assert _attempt(other, TEST_PASSWORD).status_code == 303


def test_global_progressive_delay_never_locks_out(auth_client):
    throttle = auth_client.app.state.throttle
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    throttle.sleep = fake_sleep
    for i in range(50):  # 50 failures spread across many IPs (distributed guessing)
        throttle.record_failure(f"203.0.113.{i}")
    assert throttle.delay_seconds() == 2.0

    assert _attempt(auth_client, "wrong").status_code == 401  # the 51st failure
    assert sleeps == [2.0]
    assert _attempt(auth_client, TEST_PASSWORD).status_code == 303  # still gets in
    assert sleeps == [2.0, 4.0]


def test_concurrent_wrong_attempts_cannot_exceed_the_per_ip_limit(auth_settings, monkeypatch):
    release = threading.Event()
    entered: list[int] = []

    def blocked_verify(conn, password):
        entered.append(1)
        release.wait(10)
        return False

    monkeypatch.setattr(passwords, "verify_password", blocked_verify)
    statuses: list[int] = []
    app = create_app(auth_settings)
    with TestClient(app, headers={"Origin": ORIGIN}, client=("198.51.100.7", 1)) as client:
        threads = [
            threading.Thread(target=lambda: statuses.append(_attempt(client, "wrong").status_code))
            for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        try:
            deadline = time.monotonic() + 5
            while statuses.count(429) < 3 and time.monotonic() < deadline:
                time.sleep(0.01)
            assert statuses.count(429) == 3 and len(entered) == 5
        finally:
            release.set()
            for thread in threads:
                thread.join()
    assert sorted(statuses) == [401] * 5 + [429] * 3

"""First-run /setup (login name + password) with the one-time log token (AC-28)."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.auth import passwords
from app.db.connection import connect
from app.main import create_app
from tests.conftest import ORIGIN


@pytest.fixture
def settings(make_settings):
    """A fresh install: no HOUSEHOLD_USERNAME / HOUSEHOLD_PASSWORD."""
    return make_settings(household_username=None)


@pytest.fixture
def fresh(settings, caplog):
    """No HOUSEHOLD_PASSWORD and no stored hash: the app is in setup mode."""
    caplog.set_level(logging.WARNING, logger="app.auth")
    with TestClient(create_app(settings), headers={"Origin": ORIGIN}) as client:
        yield client


def _setup(client, token, password="s3cret-pass", confirm=None, username="  Home Owner "):
    return client.post(
        "/setup",
        data={
            "token": token,
            "username": username,
            "password": password,
            "confirm": confirm or password,
        },
        follow_redirects=False,
    )


def test_setup_token_is_generated_and_logged(fresh, caplog):
    token = fresh.app.state.setup_token
    assert token and len(token) >= 43  # 32 random bytes, urlsafe base64
    assert any(f"SETUP TOKEN: {token}" in r.getMessage() for r in caplog.get_records("setup"))


def test_everything_redirects_to_setup_while_no_password(fresh):
    for path in ("/", "/login", "/l/ABC123?x=1"):
        response = fresh.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"] == "/setup"
    assert fresh.get("/api/export.json").status_code == 401
    assert fresh.post("/login", data={"password": "x"}, follow_redirects=False).headers[
        "location"
    ] == ("/setup")
    assert fresh.get("/setup").status_code == 200


def test_setup_without_token_is_403(fresh):
    response = fresh.post(
        "/setup", data={"password": "pw", "confirm": "pw"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert _setup(fresh, "wrong-token").status_code == 403
    assert fresh.app.state.setup_token is not None


def test_setup_rejects_mismatched_or_empty_password(fresh):
    token = fresh.app.state.setup_token
    assert _setup(fresh, token, "one", "two").status_code == 400
    assert _setup(fresh, token, "", "").status_code == 400


@pytest.mark.parametrize("username", ["", "   ", "x" * 65, "bad\nname"])
def test_setup_requires_a_valid_login_name(fresh, settings, username):
    response = _setup(fresh, fresh.app.state.setup_token, username=username)
    assert response.status_code == 400
    assert "Choose a login name" in response.text
    conn = connect(settings.db_path)
    try:
        assert passwords.get_username(conn) is None
        assert not passwords.has_password(conn)
    finally:
        conn.close()


def test_setup_form_asks_for_name_and_password(fresh):
    page = fresh.get("/setup").text
    assert 'name="username"' in page and 'autocomplete="username"' in page
    assert 'name="password"' in page and 'name="token"' in page


def test_setup_with_token_sets_argon2id_hash_and_logs_in(fresh, settings):
    token = fresh.app.state.setup_token
    response = _setup(fresh, token, "s3cret-pass")
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "inv_session=" in response.headers["set-cookie"]
    assert fresh.app.state.setup_token is None  # discarded after success

    conn = connect(settings.db_path)
    try:
        stored = passwords.get_password_hash(conn)
        assert stored.startswith("$argon2id$")
        assert "s3cret-pass" not in stored
        assert passwords.get_username(conn) == "Home Owner"  # trimmed
    finally:
        conn.close()

    assert fresh.get("/", follow_redirects=False).status_code == 200  # logged in
    assert fresh.get("/setup").status_code == 404
    assert _setup(fresh, token).status_code == 404

    fresh.post("/logout")
    login = fresh.post(
        "/login",
        data={"username": "home owner", "password": "s3cret-pass"},
        follow_redirects=False,
    )
    assert login.status_code == 303
    wrong_name = fresh.post(
        "/login", data={"username": "nobody", "password": "s3cret-pass"}, follow_redirects=False
    )
    assert wrong_name.status_code == 401


def test_no_setup_token_when_env_password_set(auth_client):
    assert auth_client.app.state.setup_token is None
    assert auth_client.get("/setup").status_code == 404


def test_setup_reports_the_first_error_only(fresh):
    token = fresh.app.state.setup_token
    response = _setup(fresh, token, "one", "two", username="bad\nname")
    assert response.status_code == 400
    assert "do not match" in response.text
    assert "Choose a login name" not in response.text


def test_second_setup_loses_the_race_with_404_and_overwrites_nothing(fresh, settings, monkeypatch):
    token = fresh.app.state.setup_token
    conn = connect(settings.db_path)
    try:
        assert passwords.set_credentials(conn, "First", "first-pass")
        assert not passwords.set_credentials(conn, "Second", "second-pass")
        # The route's early has_password check ran before the first setup committed.
        monkeypatch.setattr(passwords, "has_password", lambda _conn: False)
        assert _setup(fresh, token, "second-pass", username="Second").status_code == 404
        assert passwords.get_username(conn) == "First"
        assert passwords.verify_password(conn, "first-pass")
    finally:
        conn.close()


def test_setup_with_env_username_locks_the_field_and_ignores_a_submitted_name(
    make_settings, caplog
):
    settings = make_settings(household_username="Attic")
    with TestClient(create_app(settings), headers={"Origin": ORIGIN}) as client:
        page = client.get("/setup").text
        assert 'value="Attic"' in page and "readonly" in page
        assert "Set by HOUSEHOLD_USERNAME in .env" in page
        response = _setup(client, client.app.state.setup_token, username="Other")
        assert response.status_code == 303
    conn = connect(settings.db_path)
    try:
        assert passwords.get_username(conn) == "Attic"
    finally:
        conn.close()


def test_setup_form_is_editable_without_env_username(fresh):
    page = fresh.get("/setup").text
    assert "readonly" not in page and "HOUSEHOLD_USERNAME" not in page

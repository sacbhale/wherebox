"""First-run /setup with the one-time log token (AC-28)."""

import logging

import pytest
from fastapi.testclient import TestClient

from app.auth import passwords
from app.db.connection import connect
from app.main import create_app
from tests.conftest import ORIGIN


@pytest.fixture
def fresh(settings, caplog):
    """No HOUSEHOLD_PASSWORD and no stored hash: the app is in setup mode."""
    caplog.set_level(logging.WARNING, logger="app.auth")
    with TestClient(create_app(settings), headers={"Origin": ORIGIN}) as client:
        yield client


def _setup(client, token, password="s3cret-pass", confirm=None):
    return client.post(
        "/setup",
        data={"token": token, "password": password, "confirm": confirm or password},
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
    finally:
        conn.close()

    assert fresh.get("/", follow_redirects=False).status_code == 200  # logged in
    assert fresh.get("/setup").status_code == 404
    assert _setup(fresh, token).status_code == 404


def test_no_setup_token_when_env_password_set(auth_client):
    assert auth_client.app.state.setup_token is None
    assert auth_client.get("/setup").status_code == 404

"""CSRF origin checks (AC-31) and the require_htmx dependency."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.auth.csrf import origin_pair, require_htmx
from app.main import create_app
from tests.conftest import ORIGIN, TEST_PASSWORD


def _login(client: TestClient, **headers: str):
    return client.post(
        "/login", data={"password": TEST_PASSWORD}, headers=headers, follow_redirects=False
    )


@pytest.fixture
def anon(auth_settings):
    with TestClient(create_app(auth_settings)) as test_client:
        yield test_client


def test_cross_origin_post_is_refused(anon):
    assert _login(anon, Origin="http://evil.example").status_code == 403


def test_same_origin_post_is_allowed(anon):
    assert _login(anon, Origin=ORIGIN).status_code == 303


@pytest.mark.parametrize(
    "origin",
    [
        "http://testserver:8080",  # explicit non-default port
        "http://testserver:443",
        "https://testserver",  # scheme changes the default port (443 vs Host's 80)
        "http://evil.example:80",
        "null",
        "not a url",
    ],
)
def test_origin_mismatch_including_explicit_port_is_refused(anon, origin):
    assert _login(anon, Origin=origin).status_code == 403


def test_explicit_default_port_matches(anon):
    assert _login(anon, Origin="http://testserver:80").status_code == 303


def test_missing_origin_and_referer_is_refused(anon):
    assert _login(anon).status_code == 403


def test_referer_is_used_when_origin_absent(anon):
    assert _login(anon, Referer="http://testserver/login?next=/").status_code == 303
    assert _login(anon, Referer="http://evil.example/login").status_code == 403


def test_origin_takes_precedence_over_referer(anon):
    response = _login(anon, Origin="http://evil.example", Referer="http://testserver/login")
    assert response.status_code == 403


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "POST"])
def test_all_unsafe_methods_are_checked(auth_client, method):
    response = auth_client.request(
        method, "/api/items/1", headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403


def test_safe_methods_are_not_origin_checked(auth_client):
    response = auth_client.get("/api/export.json", headers={"Origin": "http://evil.example"})
    assert response.status_code == 200


def test_https_origin_matches_host_with_explicit_443(make_settings):
    settings = make_settings(
        household_password=TEST_PASSWORD, public_base_url="https://other.example"
    )
    with TestClient(create_app(settings), base_url="https://h") as client:
        response = _login(client, Origin="https://h", Host="h:443")
        assert response.status_code == 303


def test_public_base_url_host_is_accepted_when_host_differs(make_settings):
    settings = make_settings(
        household_password=TEST_PASSWORD, public_base_url="https://inv.example.com"
    )
    with TestClient(create_app(settings)) as client:
        response = _login(client, Origin="https://inv.example.com", Host="10.0.0.2:8000")
        assert response.status_code == 303
        # Same host but a different port from PUBLIC_BASE_URL does not match.
        response = _login(client, Origin="https://inv.example.com:8443", Host="10.0.0.2:8000")
        assert response.status_code == 403


def test_allowed_hosts_entries(make_settings):
    settings = make_settings(
        household_password=TEST_PASSWORD, allowed_hosts="nas.lan, Other.Example:8000"
    )
    with TestClient(create_app(settings)) as client:
        for origin in ("http://nas.lan", "https://nas.lan", "http://other.example:8000"):
            assert _login(client, Origin=origin).status_code == 303, origin
        for origin in ("http://nas.lan:8000", "http://other.example"):
            assert _login(client, Origin=origin).status_code == 403, origin


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://H.example", ("h.example", 443)),
        ("http://h.example", ("h.example", 80)),
        ("https://h.example:443/path", ("h.example", 443)),
        ("http://h.example:8000", ("h.example", 8000)),
        ("http://[::1]:8000", ("::1", 8000)),
        ("ftp://h.example", None),
        ("null", None),
        ("http://h.example:notaport", None),
    ],
)
def test_origin_pair_normalization(url, expected):
    assert origin_pair(url) == expected


def test_require_htmx_dependency():
    app = FastAPI()

    @app.post("/thing", dependencies=[Depends(require_htmx)])
    def thing() -> dict[str, bool]:
        return {"ok": True}

    client = TestClient(app)
    assert client.post("/thing").status_code == 403
    assert client.post("/thing", headers={"HX-Request": "false"}).status_code == 403
    assert client.post("/thing", headers={"HX-Request": "true"}).status_code == 200

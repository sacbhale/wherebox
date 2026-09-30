"""AC-31: trusted-proxy handling with the pinned uvicorn, configured as the entrypoint does.

docker-entrypoint.sh runs ``uvicorn app.main:create_app --factory --proxy-headers`` and uvicorn
reads ``FORWARDED_ALLOW_IPS`` from the environment. ``uvicorn.Config(...).load()`` builds the
same ASGI stack, so these tests exercise uvicorn's real ProxyHeadersMiddleware (CIDR included).
"""

from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from tests.conftest import TEST_PASSWORD

ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker-entrypoint.sh"
TRUSTED = "127.0.0.1,172.16.0.0/12"
FORWARDED = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "203.0.113.9"}


def test_entrypoint_enables_proxy_headers_and_leaves_trust_to_env():
    script = ENTRYPOINT.read_text()
    assert "--proxy-headers" in script
    assert "--forwarded-allow-ips" not in script  # FORWARDED_ALLOW_IPS env var decides


@pytest.fixture
def loaded_app(monkeypatch, data_dir):
    """The app exactly as the entrypoint loads it (factory + --proxy-headers + env trust)."""
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://inv.example.com")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("WORKER_AUTOSTART", "false")
    monkeypatch.setenv("AI_PROVIDER", "fake")
    monkeypatch.setenv("HOUSEHOLD_PASSWORD", TEST_PASSWORD)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", TRUSTED)
    config = uvicorn.Config("app.main:create_app", factory=True, proxy_headers=True)
    config.load()
    assert type(config.loaded_app).__name__ == "ProxyHeadersMiddleware"
    return config.loaded_app


def test_uvicorn_cidr_trust_honors_only_trusted_clients():
    """Direct check of the pinned uvicorn: 172.18.0.5 is inside 172.16.0.0/12, 10.0.0.5 is not."""
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen = {}

    async def echo(scope, receive, send):
        seen["scheme"], seen["client"] = scope["scheme"], scope["client"][0]
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = ProxyHeadersMiddleware(echo, trusted_hosts=TRUSTED)
    TestClient(app, client=("172.18.0.5", 40000)).get("/", headers=FORWARDED)
    assert seen == {"scheme": "https", "client": "203.0.113.9"}
    TestClient(app, client=("10.0.0.5", 40000)).get("/", headers=FORWARDED)
    assert seen == {"scheme": "http", "client": "10.0.0.5"}


def _login(app, client_ip: str, headers: dict[str, str]):
    with TestClient(app, client=(client_ip, 40000)) as tc:
        return tc.post(
            "/login",
            data={"password": TEST_PASSWORD},
            headers={"Host": "inv.example.com", **headers},
            follow_redirects=False,
        )


def test_cookie_secure_auto_set_only_for_trusted_forwarded_https(loaded_app):
    trusted = _login(loaded_app, "172.18.0.5", {**FORWARDED, "Origin": "https://inv.example.com"})
    assert trusted.status_code == 303
    assert "secure" in trusted.headers["set-cookie"].lower()

    # Untrusted client: X-Forwarded-Proto is ignored, the effective scheme stays http.
    untrusted = _login(loaded_app, "10.0.0.5", {**FORWARDED, "Origin": "http://inv.example.com"})
    assert untrusted.status_code == 303
    assert "secure" not in untrusted.headers["set-cookie"].lower()

    # Plain-HTTP LAN access without a proxy also gets a working (non-Secure) cookie.
    lan = _login(loaded_app, "192.168.1.20", {"Origin": "http://inv.example.com"})
    assert lan.status_code == 303
    assert "secure" not in lan.headers["set-cookie"].lower()


def test_forwarded_scheme_drives_csrf_default_port(loaded_app):
    """Behind a trusted https proxy, Origin https://inv.example.com matches Host inv.example.com."""
    response = _login(
        loaded_app, "172.18.0.5", {**FORWARDED, "Origin": "https://inv.example.com:443"}
    )
    assert response.status_code == 303


def test_throttle_keys_on_forwarded_ip_only_from_trusted_proxy(loaded_app):
    with TestClient(loaded_app, client=("172.18.0.5", 40000)) as proxy:

        def attempt(forwarded_for: str) -> int:
            return proxy.post(
                "/login",
                data={"password": "wrong"},
                headers={
                    "Host": "inv.example.com",
                    "Origin": "https://inv.example.com",
                    "X-Forwarded-Proto": "https",
                    "X-Forwarded-For": forwarded_for,
                },
                follow_redirects=False,
            ).status_code

        assert [attempt("203.0.113.9") for _ in range(5)] == [401] * 5
        assert attempt("203.0.113.9") == 429
        assert attempt("198.51.100.7") == 401  # a different real client is unaffected

    with TestClient(loaded_app, client=("10.0.0.5", 40000)) as direct:
        # Spoofed X-Forwarded-For from an untrusted address is ignored: 10.0.0.5 is the key.
        codes = [
            direct.post(
                "/login",
                data={"password": "wrong"},
                headers={
                    "Host": "inv.example.com",
                    "Origin": "http://inv.example.com",
                    "X-Forwarded-For": f"198.51.100.{i}",
                },
                follow_redirects=False,
            ).status_code
            for i in range(6)
        ]
        assert codes == [401] * 5 + [429]

"""End-to-end fixtures: a real uvicorn server per test and iPhone-13-sized browser contexts.

Every test gets its own server (fresh DATA_DIR, fresh fake provider), so the scripted fake AI
steps are consumed from the start. ``@pytest.mark.fake_steps(0, {...})`` picks the script:
ints are indexes into tests/fixtures/fake_script.json, dicts are literal steps.
``@pytest.mark.fake_models({"<model id>": {"steps": [...]}})`` adds per-model scripts.

Browsers: Chromium and WebKit both run by default (``--browser`` narrows it). They are launched
locally when the host can run them; otherwise (e.g. Ubuntu 20.04, which Playwright no longer
supports) the official Playwright image runs them in Docker with host networking, and the tests
connect to it. ``PLAYWRIGHT_WS_ENDPOINT`` points at an already running Playwright server instead.
"""

import contextlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path

import httpx
import playwright as playwright_pkg
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, Playwright

from app.config import Settings

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"
USERNAME = "test"
PASSWORD = "test"
VIEWPORT = {"width": 390, "height": 844}
DEFAULT_BROWSERS = ["chromium", "webkit"]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "fake_steps(*steps): fake AI script steps (fixture index or step dict)"
    )
    config.addinivalue_line(
        "markers", "fake_models(scripts): per-model fake AI scripts ({id: {steps: [...]}})"
    )
    if not config.option.browser:
        config.option.browser = list(DEFAULT_BROWSERS)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_for_port(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", port), 0.5):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------- browsers


def _launch_error(pw: Playwright, names: list[str]) -> str | None:
    for name in names:
        try:
            getattr(pw, name).launch().close()
        except PlaywrightError as exc:
            return f"{name}: {exc}"
    return None


@pytest.fixture(scope="session")
def connect_options(pytestconfig: pytest.Config, playwright: Playwright) -> Iterator[dict | None]:
    """pytest-playwright hook: None launches locally, a dict connects to a Playwright server."""
    endpoint = os.environ.get("PLAYWRIGHT_WS_ENDPOINT")
    if endpoint:
        yield {"endpoint": endpoint}
        return
    local_error = _launch_error(playwright, pytestconfig.option.browser)
    if local_error is None:
        yield None
        return
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.fail(
            "Browsers cannot run on this host and Docker is unavailable to run them.\n"
            f"Local launch failed with: {local_error}",
            pytrace=False,
        )
    pw_version = version("playwright")
    driver = Path(playwright_pkg.__file__).parent / "driver"
    port = _free_port()
    name = f"inventory-e2e-playwright-{uuid.uuid4().hex[:8]}"
    # The image carries the browsers of this exact Playwright version; the host's own driver
    # (same version) serves them.
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name, "--network", "host", "--init",
            "--user", "pwuser", "-v", f"{driver}:/pwdriver:ro",
            f"mcr.microsoft.com/playwright/python:v{pw_version}-noble",
            "/pwdriver/node", "/pwdriver/package/cli.js",
            "run-server", "--port", str(port), "--host", "127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=600,
    )  # fmt: skip
    try:
        if not _wait_for_port(port, 60):
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
            pytest.fail(f"Playwright server in Docker did not start:\n{logs.stdout}{logs.stderr}")
        yield {"endpoint": f"ws://127.0.0.1:{port}/"}
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="session")
def browser_context_args(playwright: Playwright) -> dict:
    """iPhone 13 (touch, mobile, 3x) at a 390x844 viewport, for both engines."""
    device = dict(playwright.devices["iPhone 13"])
    device.pop("default_browser_type", None)
    device["viewport"] = dict(VIEWPORT)
    device["screen"] = dict(VIEWPORT)
    return device


# ---------------------------------------------------------------- server


@dataclass
class LiveServer:
    base_url: str
    data_dir: Path
    http: httpx.Client = field(repr=False)

    def url(self, path: str) -> str:
        return self.base_url + path

    def query(self, sql: str, *params: object) -> list[sqlite3.Row]:
        with contextlib.closing(sqlite3.connect(self.data_dir / "inventory.db")) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()

    def create_location(self, name: str, parent_id: int | None = None) -> tuple[int, str]:
        """Create a location through the form POST. Returns (id, short code)."""
        data = {"name": name} | ({"parent_id": str(parent_id)} if parent_id else {})
        response = self.http.post("/locations", data=data)
        assert response.status_code == 303, response.text
        code = response.headers["location"].removeprefix("/l/")
        (row,) = self.query("SELECT id FROM locations WHERE short_code = ?", code)
        return row["id"], code

    def create_item(self, name: str, location_id: int, quantity: int = 1) -> int:
        """Create an item through the manual form POST. Returns its id."""
        data = {"name": name, "quantity": str(quantity), "kind": "belonging"}
        response = self.http.post("/items/new", data=data | {"location_id": str(location_id)})
        assert response.status_code == 303, response.text
        (row,) = self.query("SELECT max(id) AS id FROM items WHERE name = ?", name)
        return row["id"]

    def capture_photo(self, kind: str, location_id: int, photo: Path) -> None:
        """Queue a photo or reconcile capture over HTTP and wait until the worker is done."""
        response = self.http.post(
            f"/captures/{kind}",
            data={"location_id": str(location_id)},
            files={"photo": (photo.name, photo.read_bytes(), "image/jpeg")},
            headers={"HX-Request": "true"},
        )
        assert response.status_code == 200, response.text
        self.wait_for_captures()
        assert not self.query("SELECT 1 FROM captures WHERE status = 'failed'")

    def wait_for_captures(self, timeout: float = 20) -> None:
        """Wait until the worker has finished every queued capture."""
        deadline = time.monotonic() + timeout
        while self.query("SELECT 1 FROM captures WHERE status IN ('queued', 'processing')"):
            assert time.monotonic() < deadline, "capture did not finish"
            time.sleep(0.1)

    def login(self, page: Page, path: str = "/") -> None:
        page.goto(self.url(f"/login?next={path}"))
        page.fill("#username", USERNAME)
        page.fill("#password", PASSWORD)
        page.locator("button[type=submit]").tap()
        page.wait_for_url(self.url(path))


def _script(marker: pytest.Mark | None, models: pytest.Mark | None, directory: Path) -> Path:
    fixture = FIXTURES / "fake_script.json"
    if marker is None and models is None:
        return fixture
    base = json.loads(fixture.read_text())
    if marker is not None:
        steps = [base["steps"][s] if isinstance(s, int) else s for s in marker.args]
        base |= {"steps": steps, "repeat_last": False}
    if models is not None:
        base["models"] = models.args[0]
    path = directory / "fake_script.json"
    path.write_text(json.dumps(base))
    return path


@pytest.fixture
def server(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[LiveServer]:
    """A uvicorn server with the fake AI provider (models from the test catalog, AI_MODEL
    test/model-a) and a running worker, and a logged-in
    httpx client (sending the matching Origin) for seeding data."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env = {k: v for k, v in os.environ.items() if k.lower() not in Settings.model_fields}
    env |= {
        "DATA_DIR": str(data_dir),
        "AI_PROVIDER": "fake",
        "FAKE_AI_SCRIPT": str(
            _script(
                request.node.get_closest_marker("fake_steps"),
                request.node.get_closest_marker("fake_models"),
                tmp_path,
            )
        ),
        # Test-only OpenRouter catalog: shows the Settings model picker under the fake provider.
        "OPENROUTER_CATALOG_FILE": str(FIXTURES / "openrouter" / "models.json"),
        "AI_MODEL": "test/model-a",
        "PUBLIC_BASE_URL": base_url,
        "HOUSEHOLD_USERNAME": USERNAME,
        "HOUSEHOLD_PASSWORD": PASSWORD,
        "WORKER_AUTOSTART": "true",
    }
    log_path = tmp_path / "server.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
    http = httpx.Client(base_url=base_url, headers={"Origin": base_url}, timeout=10)
    try:
        deadline = time.monotonic() + 30
        while True:
            with contextlib.suppress(httpx.TransportError):
                if http.get("/healthz").status_code == 200:
                    break
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"server did not start:\n{log_path.read_text()}")
            time.sleep(0.1)
        response = http.post(
            "/login", data={"username": USERNAME, "password": PASSWORD, "next": "/"}
        )
        assert response.status_code == 303, response.text
        yield LiveServer(base_url, data_dir, http)
    finally:
        http.close()
        process.terminate()
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


# ---------------------------------------------------------------- taps


class Taps:
    """Performs taps and counts them, so tests can assert how many a flow took (AC-6)."""

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, locator: Locator) -> None:
        locator.tap()
        self.count += 1

    def reset(self) -> None:
        self.count = 0


@pytest.fixture
def tap() -> Taps:
    return Taps()

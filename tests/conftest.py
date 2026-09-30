import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db.connection import connect
from app.db.migrate import migrate
from app.main import create_app

SettingsFactory = Callable[..., Settings]


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's shell environment out of Settings."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


@pytest.fixture
def make_settings(data_dir: Path) -> SettingsFactory:
    """Settings for tests; keyword overrides use field names (e.g. monthly_ai_budget=5)."""

    def factory(**overrides: object) -> Settings:
        values: dict[str, object] = {
            "public_base_url": "http://testserver",
            "data_dir": data_dir,
            "ai_provider": "fake",
            "worker_autostart": False,
        }
        values.update(overrides)
        return Settings(**values)

    return factory


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    return make_settings()


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    """A connection to a freshly migrated database in the temp DATA_DIR."""
    connection = connect(settings.db_path)
    migrate(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """TestClient with the lifespan run (migrations applied)."""
    with TestClient(create_app(settings)) as test_client:
        yield test_client


TEST_PASSWORD = "correct horse battery staple"
# The TestClient's origin; unsafe requests must send it (CSRF origin check).
ORIGIN = "http://testserver"


@pytest.fixture
def auth_settings(make_settings: SettingsFactory) -> Settings:
    """Settings with HOUSEHOLD_PASSWORD=TEST_PASSWORD."""
    return make_settings(household_password=TEST_PASSWORD)


@contextmanager
def logged_in(settings: Settings, **client_kwargs: Any) -> Iterator[TestClient]:
    """A logged-in TestClient for ``settings`` (household_password must be TEST_PASSWORD).

    It sends ``Origin: http://testserver`` unless ``headers`` is given; htmx mutation
    endpoints also need ``headers={"HX-Request": "true"}`` per request.
    """
    client_kwargs.setdefault("headers", {"Origin": ORIGIN})
    with TestClient(create_app(settings), **client_kwargs) as test_client:
        response = test_client.post(
            "/login", data={"password": TEST_PASSWORD}, follow_redirects=False
        )
        assert response.status_code == 303, response.text
        yield test_client


@pytest.fixture
def auth_client(auth_settings: Settings) -> Iterator[TestClient]:
    with logged_in(auth_settings) as test_client:
        yield test_client

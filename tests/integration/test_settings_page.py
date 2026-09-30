"""GET /settings renders the AI ledger totals, budget, provider, URL warning, backup and photos."""

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.ai.schemas import Usage
from app.capture.budget import month_window, record_ai_call
from app.config import Settings
from app.db.connection import write_tx
from app.main import create_app
from app.settings_info import QR_WARNING, build_settings_info, clear_photo_size_cache
from tests.conftest import TEST_PASSWORD, SettingsFactory, logged_in
from tests.inventory_helpers import open_db


@pytest.fixture(autouse=True)
def _fresh_size_cache() -> Iterator[None]:
    clear_photo_size_cache()
    yield
    clear_photo_size_cache()


@pytest.fixture
def auth_settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(
        household_password=TEST_PASSWORD,
        tz="Asia/Kolkata",
        monthly_ai_budget=5,
        ai_input_price_per_mtok=3,
        ai_output_price_per_mtok=15,
        ai_model="fake-vision",
        public_base_url="https://inv.example.com",
    )


def _seed(settings: Settings) -> None:
    now = datetime.now(UTC)
    with open_db(settings) as conn:
        record_ai_call(
            conn, settings, None, now, Usage(input_tokens=100_000, output_tokens=2_000), "ok"
        )
        record_ai_call(
            conn, settings, None, now, Usage(input_tokens=500, output_tokens=0), "invalid_output"
        )
        record_ai_call(conn, settings, None, now, None, "timeout")
        # Last month's usage must not count.
        record_ai_call(
            conn, settings, None, now - timedelta(days=40), Usage(input_tokens=999_999), "ok"
        )
        with write_tx(conn):
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('last_backup_at', ?)",
                ("2026-09-28T03:00:00.000Z",),
            )
    photo = settings.photos_dir / "2026" / "09" / "a.jpg"
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b"x" * 3 * 1024 * 1024)


def test_settings_page_shows_every_field(auth_client: TestClient, auth_settings: Settings) -> None:
    _seed(auth_settings)
    response = auth_client.get("/settings")
    assert response.status_code == 200
    html = response.text
    assert "100,500 input tokens" in html
    assert "2,000 output tokens" in html
    # (100,500 * 3 + 2,000 * 15) micros = 331,500 micros = $0.33
    assert "$0.33 USD" in html
    assert "Budget: $5.00 USD" in html
    assert "Remaining: $4.67 USD" in html
    assert "fake / fake-vision" in html
    assert "https://inv.example.com" in html
    assert QR_WARNING in html
    assert "2026-09-28T03:00:00.000Z" in html
    assert "3.0 MB" in html
    assert "Timeouts are not counted" in html
    assert "Asia/Kolkata" in html


def test_settings_sum_equals_ledger(auth_settings: Settings, auth_client: TestClient) -> None:
    _seed(auth_settings)
    with open_db(auth_settings) as conn:
        info = build_settings_info(conn, auth_settings)
        rows = conn.execute("SELECT started_at, cost_micros FROM ai_calls").fetchall()
    start, end = month_window(auth_settings, datetime.now(UTC))
    assert info.usage.cost_micros == sum(c for s, c in rows if start <= s < end) == 331_500
    assert not info.budget_exhausted


def test_settings_requires_login(auth_settings: Settings) -> None:
    with TestClient(create_app(auth_settings)) as client:
        response = client.get("/settings", follow_redirects=False)
    assert response.status_code == 303
    assert unquote(response.headers["location"]) == "/login?next=/settings"


def test_ai_disabled_and_no_budget(make_settings: SettingsFactory) -> None:
    settings = make_settings(household_password=TEST_PASSWORD, ai_provider="anthropic")
    with logged_in(settings) as client:
        html = client.get("/settings").text
    assert "AI disabled" in html
    assert "AI_API_KEY" in html
    assert "No budget set (USD)" in html
    assert "Never" in html  # no backup yet
    assert "No backup has been written yet." in html
    assert "0 B" in html


def test_budget_reached_warning(make_settings: SettingsFactory, conn: sqlite3.Connection) -> None:
    settings = make_settings(
        monthly_ai_budget=0, ai_input_price_per_mtok=1, ai_output_price_per_mtok=1
    )
    info = build_settings_info(conn, settings)
    assert info.budget_exhausted and info.remaining_usd == 0
    assert any("budget reached" in warning for warning in info.warnings)

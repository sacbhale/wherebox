import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:8000", "http://localhost:8000"),
        ("https://inv.example.com", "https://inv.example.com"),
        ("https://inv.example.com/", "https://inv.example.com"),
        ("http://192.168.1.10:8080", "http://192.168.1.10:8080"),
        ("http://[::1]:8000", "http://[::1]:8000"),
    ],
)
def test_public_base_url_accepts_scheme_host_port(raw, expected):
    assert Settings(public_base_url=raw).public_base_url == expected


@pytest.mark.parametrize(
    "raw",
    [
        "https://inv.example.com/inventory",
        "https://inv.example.com/inventory/",
        "https://inv.example.com//",
        "inv.example.com",
        "ftp://inv.example.com",
        "https://",
        "https://inv.example.com?x=1",
        "https://inv.example.com#frag",
        "https://inv.example.com?",
        "https://user:pw@inv.example.com",
        "https://inv.example.com:99999",
        "https://inv.example.com:abc",
    ],
)
def test_public_base_url_rejects_invalid(raw):
    with pytest.raises(ValidationError, match="PUBLIC_BASE_URL"):
        Settings(public_base_url=raw)


def test_public_base_url_is_required():
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings()


def test_env_vars_are_read_and_empty_values_ignored(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://inv.example.com/")
    monkeypatch.setenv("DATA_DIR", "/tmp/somewhere")
    monkeypatch.setenv("WORKER_AUTOSTART", "false")
    monkeypatch.setenv("AI_API_KEY", "")
    monkeypatch.setenv("MONTHLY_AI_BUDGET", "")
    s = Settings()
    assert s.public_base_url == "https://inv.example.com"
    assert str(s.db_path) == "/tmp/somewhere/inventory.db"
    assert s.worker_autostart is False
    assert s.ai_api_key is None
    assert s.monthly_ai_budget is None


def test_defaults():
    s = Settings(public_base_url="http://localhost:8000")
    assert (s.puid, s.pgid, s.session_days, s.cookie_secure) == (1000, 1000, 30, "auto")
    assert (s.ai_confidence_threshold, s.ai_timeout_seconds, s.ai_max_image_edge) == (0.7, 60, 1500)
    assert s.max_upload_mb == 25
    assert s.forwarded_allow_ips == "127.0.0.1,172.16.0.0/12"
    assert s.worker_autostart is True
    assert s.backup_interval_hours == 24
    assert s.tz == "UTC"


def test_budget_requires_prices():
    with pytest.raises(ValidationError, match="MONTHLY_AI_BUDGET requires"):
        Settings(public_base_url="http://x", monthly_ai_budget=5)
    with pytest.raises(ValidationError, match="MONTHLY_AI_BUDGET requires"):
        Settings(public_base_url="http://x", monthly_ai_budget=5, ai_input_price_per_mtok=3)
    s = Settings(
        public_base_url="http://x",
        monthly_ai_budget=5,
        ai_input_price_per_mtok=3,
        ai_output_price_per_mtok=15,
    )
    assert s.monthly_ai_budget == 5


def test_unknown_tz_rejected():
    with pytest.raises(ValidationError, match="unknown TZ"):
        Settings(public_base_url="http://x", tz="Mars/Olympus")


def test_allowed_hosts_list():
    s = Settings(public_base_url="http://x", allowed_hosts=" Nas.local , inv.example.com,")
    assert s.allowed_hosts_list == ["nas.local", "inv.example.com"]

import pytest
from pydantic import ValidationError

from app.db.connection import connect
from app.main import create_app


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_migrates_and_creates_data_dirs(client, settings):
    assert settings.photos_dir.is_dir()
    assert settings.backups_dir.is_dir()
    conn = connect(settings.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM locations WHERE is_system = 1").fetchone()[0] == 1
    finally:
        conn.close()


def test_create_app_reads_env(monkeypatch, data_dir):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://localhost:8000")
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    monkeypatch.setenv("WORKER_AUTOSTART", "false")
    settings = create_app().state.settings
    assert settings.data_dir == data_dir
    assert settings.worker_autostart is False


def test_create_app_fails_fast_on_bad_public_base_url(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://inv.example.com/sub")
    with pytest.raises(ValidationError, match="no path"):
        create_app()

import time

import pytest
from pydantic import ValidationError

from app.db.connection import connect
from app.main import create_app


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_static_range_requests_are_answered_quickly(client):
    """Range handling on /static (Starlette's FileResponse; Starlette 0.49.1 fixed quadratic
    Range parsing): a valid range is a 206 slice, an unsatisfiable one a 416, and a header with
    thousands of ranges is answered without slow parsing."""
    full = client.get("/static/app.css").content
    part = client.get("/static/app.css", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and part.content == full[:10]
    assert part.headers["content-range"] == f"bytes 0-9/{len(full)}"
    beyond = client.get("/static/app.css", headers={"Range": f"bytes={len(full) + 10}-"})
    assert beyond.status_code == 416
    assert beyond.headers["content-range"] == f"bytes */{len(full)}"
    started = time.perf_counter()
    many = client.get("/static/app.css", headers={"Range": "bytes=" + "0-1," * 20_000 + "0-1"})
    assert many.status_code in (200, 206, 416)
    assert time.perf_counter() - started < 2


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

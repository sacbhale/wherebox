import re
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


def test_brand_comes_from_app_name(auth_client, monkeypatch):
    # Page titles, the header brand and the home heading all come from APP_NAME, so a rename is
    # one line. Swap the template global and check every place follows it.
    from app import config
    from app.templating import templates

    assert config.APP_NAME == "Wherebox"
    monkeypatch.setitem(templates.env.globals, "app_name", "Testbox")
    home = auth_client.get("/").text
    assert "<title>Testbox</title>" in home
    assert re.search(r'<a class="brand" href="/"><svg[^>]*>.*?</svg>Testbox</a>', home)
    assert "<h1>Testbox</h1>" in home
    assert "<title>Search · Testbox</title>" in auth_client.get("/search").text
    assert "Inventory" not in home


def test_pages_link_versioned_assets(auth_client):
    # Phones kept a stale app.css after a deploy; every asset link now carries its content hash.
    import re

    html = auth_client.get("/").text
    for path in ("app.css", "app.js", "vendor/htmx.min.js"):
        assert re.search(rf'"/static/{re.escape(path)}\?v=[0-9a-f]{{10}}"', html), path
    assert not re.search(r'"/static/[^"?]+"', html), "an unversioned /static link is left"


def test_asset_version_follows_the_content(tmp_path, monkeypatch):
    from app import static_assets

    (tmp_path / "a.css").write_text("body{}")
    monkeypatch.setattr(static_assets, "STATIC_DIR", tmp_path)
    static_assets._version.cache_clear()
    first = static_assets.static_url("a.css")
    (tmp_path / "a.css").write_text("body{color:red}")
    static_assets._version.cache_clear()
    second = static_assets.static_url("a.css")
    static_assets._version.cache_clear()
    assert first.startswith("/static/a.css?v=") and first != second


def test_static_cache_headers(client):
    from app.static_assets import static_url

    versioned = client.get(static_url("app.css"))
    assert versioned.status_code == 200
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/static/app.css").headers["cache-control"] == "no-cache"


def test_only_a_real_version_param_is_cached_and_304s_keep_the_header(client):
    from app.static_assets import static_url

    assert client.get("/static/app.css?nov=1").headers["cache-control"] == "no-cache"
    url = static_url("app.css")
    etag = client.get(url).headers["etag"]
    not_modified = client.get(url, headers={"If-None-Match": etag})
    assert not_modified.status_code == 304
    assert not_modified.headers["cache-control"] == "public, max-age=31536000, immutable"

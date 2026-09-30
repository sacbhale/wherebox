"""Photo capture from a location (AC-13..AC-16, AC-22, the AC-15 overhead and queue position)."""

import hashlib
import io
import re
import time

import pytest
from PIL import Image

from app.ai.fake import FakeProvider
from app.capture.worker import Worker
from app.inventory import items, locations
from tests.capture_helpers import (
    FIXTURES,
    SCRIPT,
    ai_calls,
    capture_id,
    capture_items,
    capture_row,
    post_photo,
    post_receipt,
    run,
    script_step,
)
from tests.conftest import TEST_PASSWORD, logged_in
from tests.inventory_helpers import HX, open_db


@pytest.fixture
def garage(auth_client, auth_settings) -> int:
    with open_db(auth_settings) as conn:
        location_id, _ = locations.create(conn, "Garage")
    return location_id


def test_photo_capture_saves_items_at_the_location_without_confirm(
    auth_client, auth_settings, garage
):
    response = post_photo(auth_client, garage)
    assert response.status_code == 200
    cid = capture_id(response)
    assert 'hx-trigger="every 2s"' in response.text
    assert f'hx-get="/captures/{cid}/status"' in response.text

    shelf = script_step(0)
    provider = FakeProvider([shelf])
    assert run(auth_settings, provider) is True
    assert run(auth_settings, provider) is False  # nothing else queued

    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        rows = capture_items(conn, cid)
        photo = conn.execute("SELECT * FROM photos WHERE capture_id = ?", (cid,)).fetchone()
        calls = ai_calls(conn, cid)
    assert capture["status"] == "done" and capture["error"] is None
    assert capture["provider"] == "fake" and capture["model"] == "fake-model"
    assert capture["duration_ms"] is not None and capture["finished_at"] is not None
    assert len(rows) == 12
    by_name = {row["name"]: row for row in rows}
    for extracted in shelf.items:
        row = by_name[extracted.name]
        assert row["location_id"] == garage
        assert row["quantity"] == extracted.quantity
        assert row["kind"] == extracted.kind
        assert row["confidence"] == pytest.approx(extracted.confidence)
        assert row["needs_review"] == int(extracted.confidence < 0.7)
        assert row["photo_id"] == photo["id"]
        assert row["capture_id"] == cid
    assert sum(row["needs_review"] for row in rows) == 2
    assert [(c["outcome"], c["input_tokens"], c["output_tokens"]) for c in calls] == [
        ("ok", 1800, 420)
    ]
    assert provider.requests[0].mode == "shelf"

    status = auth_client.get(f"/captures/{cid}/status")
    assert status.status_code == 286
    assert "hx-trigger" not in status.text
    assert "Added 12 items to" in status.text and "(2 to review)" in status.text

    # Saved without a confirm step: searchable right away.
    found = auth_client.get("/search", params={"q": "hammer"})
    assert "Claw hammer" in found.text and "Garage" in found.text


@pytest.mark.parametrize(("threshold", "flagged"), [(0.0, 0), (0.7, 2), (0.9, 6), (1.0, 12)])
def test_needs_review_follows_the_confidence_threshold(make_settings, threshold, flagged):
    settings = make_settings(household_password=TEST_PASSWORD, ai_confidence_threshold=threshold)
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            garage, _ = locations.create(conn, "Garage")
        cid = capture_id(post_photo(client, garage))
        run(settings, FakeProvider([script_step(0)]))
        with open_db(settings) as conn:
            rows = capture_items(conn, cid)
    assert sum(row["needs_review"] for row in rows) == flagged
    assert all(row["needs_review"] == int(row["confidence"] < threshold) for row in rows)


def test_stored_original_matches_the_upload(auth_client, auth_settings, garage):
    upload = (FIXTURES / "shelf.jpg").read_bytes()
    cid = capture_id(post_photo(auth_client, garage))
    with open_db(auth_settings) as conn:
        photo = conn.execute("SELECT * FROM photos WHERE capture_id = ?", (cid,)).fetchone()
    assert re.fullmatch(r"photos/\d{4}/\d{2}/[0-9a-f]{32}\.jpg", photo["rel_path"])
    stored = (auth_settings.data_dir / photo["rel_path"]).read_bytes()
    assert hashlib.sha256(stored).hexdigest() == photo["sha256"]
    assert photo["sha256"] == hashlib.sha256(upload).hexdigest()

    run(auth_settings, FakeProvider([script_step(0)]))
    with open_db(auth_settings) as conn:
        photo = conn.execute("SELECT * FROM photos WHERE id = ?", (photo["id"],)).fetchone()
        linked = {row["photo_id"] for row in capture_items(conn, cid)}
    assert linked == {photo["id"]}
    with Image.open(FIXTURES / "shelf.jpg") as im:
        assert (photo["width"], photo["height"]) == im.size
    assert photo["display_rel_path"] is None


def test_heic_gets_a_display_jpeg_and_keeps_the_original(auth_client, auth_settings, garage):
    upload = (FIXTURES / "iphone.heic").read_bytes()
    cid = capture_id(post_photo(auth_client, garage, FIXTURES / "iphone.heic", "image/heic"))
    run(auth_settings, FakeProvider([script_step(0)]))
    with open_db(auth_settings) as conn:
        photo = conn.execute("SELECT * FROM photos WHERE capture_id = ?", (cid,)).fetchone()
        assert capture_row(conn, cid)["status"] == "done"
    original = auth_settings.data_dir / photo["rel_path"]
    assert original.suffix == ".heic" and original.read_bytes() == upload
    assert photo["display_rel_path"].endswith(".display.jpg")
    display = auth_settings.data_dir / photo["display_rel_path"]
    with Image.open(display) as im:
        assert im.format == "JPEG"
        assert (photo["width"], photo["height"]) == im.size


@pytest.mark.parametrize("name", ["shelf.jpg", "iphone.heic", "receipt.pdf"])
def test_every_image_sent_to_the_provider_is_at_most_1500px(
    auth_client, auth_settings, garage, name
):
    path = FIXTURES / name
    if name.endswith(".pdf"):
        post_receipt(auth_client, path)
    else:
        post_photo(auth_client, garage, path)
    provider = FakeProvider([script_step(1 if name.endswith(".pdf") else 0)])
    run(auth_settings, provider)
    assert len(provider.requests) == 1
    sent = provider.requests[0].images
    assert sent
    for data in sent:
        with Image.open(io.BytesIO(data)) as im:
            assert im.format == "JPEG"
            assert max(im.size) <= 1500


def test_pipeline_overhead_under_2s_with_the_worker_thread(auth_client, auth_settings, garage):
    """AC-15: POST to done (status 286) with a 12-item fake capture and an empty queue."""
    provider = FakeProvider([script_step(0)])
    worker = Worker(auth_settings, provider)
    auth_client.app.state.worker = worker
    worker.start()
    try:
        started = time.perf_counter()
        cid = capture_id(post_photo(auth_client, garage))
        while True:
            status = auth_client.get(f"/captures/{cid}/status")
            if status.status_code == 286:
                break
            assert time.perf_counter() - started < 10, status.text
            time.sleep(0.02)
        elapsed = time.perf_counter() - started
    finally:
        worker.stop()
        auth_client.app.state.worker = None
    assert "Added 12 items" in status.text
    assert elapsed < 2.0, f"pipeline overhead {elapsed:.3f}s"


def test_waiting_captures_show_their_queue_position(auth_client, auth_settings, garage):
    first = capture_id(post_photo(auth_client, garage))
    second_response = post_photo(auth_client, garage)
    second = capture_id(second_response)
    assert "Waiting: 1 capture ahead." in second_response.text
    third = auth_client.get(f"/captures/{capture_id(post_photo(auth_client, garage))}/status")
    assert third.status_code == 200 and "Waiting: 2 captures ahead." in third.text
    assert "Starting" in auth_client.get(f"/captures/{first}/status").text

    run(auth_settings, FakeProvider([script_step(0)]))
    after = auth_client.get(f"/captures/{second}/status")
    assert after.status_code == 200 and "Starting" in after.text

    panel = auth_client.get("/captures/panel", params={"loc": garage})
    assert "Waiting: 1 capture ahead." in panel.text


def test_capture_posts_need_htmx(auth_client, garage):
    path = FIXTURES / "shelf.jpg"
    response = auth_client.post(
        "/captures/photo",
        data={"location_id": str(garage)},
        files={"photo": (path.name, path.read_bytes(), "image/jpeg")},
    )
    assert response.status_code == 403
    assert auth_client.post("/captures/text", data={"text": "milk"}).status_code == 403


def test_upload_limits_and_validation(make_settings):
    settings = make_settings(household_password=TEST_PASSWORD, max_upload_mb=1)
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            garage, _ = locations.create(conn, "Garage")
        big = b"\xff\xd8\xff" + b"0" * (1024 * 1024)
        response = client.post(
            "/captures/photo",
            headers=HX,
            data={"location_id": str(garage)},
            files={"photo": ("big.jpg", big, "image/jpeg")},
        )
        assert response.status_code == 413
        assert "1 MB" in response.text
        empty = client.post(
            "/captures/photo",
            headers=HX,
            data={"location_id": str(garage)},
            files={"photo": ("x.jpg", b"", "image/jpeg")},
        )
        assert empty.status_code == 400
        missing = client.post("/captures/photo", headers=HX, data={"location_id": str(garage)})
        assert missing.status_code == 400
        assert post_photo(client, 999_999).status_code == 400
        with open_db(settings) as conn:
            assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 0
        assert not [p for p in settings.photos_dir.rglob("*") if p.is_file()]


def test_capture_forms(auth_client, auth_settings, garage):
    with open_db(auth_settings) as conn:
        code = conn.execute("SELECT short_code FROM locations WHERE id = ?", (garage,)).fetchone()
        shed, _ = locations.create(conn, "Shed")
        items.create(conn, items.ItemData(name="Rake", location_id=shed))

    page = auth_client.get(f"/l/{code[0]}")
    assert f'hx-get="/captures/panel?loc={garage}"' in page.text

    panel = auth_client.get("/captures/panel", params={"loc": garage}).text
    assert re.search(
        r'<input type="file" name="photo" accept="image/\*" capture="environment"', panel
    )
    assert f'name="location_id" value="{garage}"' in panel
    assert 'name="text"' in panel and 'name="receipt"' not in panel

    home = auth_client.get("/captures/panel").text
    assert re.search(r'<input type="file" name="receipt" accept="image/\*,application/pdf" >', home)
    assert re.search(r'capture="environment"', home)
    assert f'<option value="{shed}" selected>' in home  # last used location
    assert 'hx-get="/captures/panel"' in auth_client.get("/").text

    standalone = auth_client.get(f"/l/{code[0]}/capture")
    assert standalone.status_code == 200 and 'capture="environment"' in standalone.text
    assert auth_client.get("/l/ZZZZZZ/capture").status_code == 404


def test_worker_autostarts_with_the_configured_provider_and_stops(make_settings):
    settings = make_settings(
        household_password=TEST_PASSWORD, worker_autostart=True, fake_ai_script=SCRIPT
    )
    with logged_in(settings) as client:
        worker = client.app.state.worker
        assert worker is not None and worker._thread.is_alive()
        with open_db(settings) as conn:
            garage, _ = locations.create(conn, "Garage")
        cid = capture_id(post_photo(client, garage))
        deadline = time.perf_counter() + 10
        while client.get(f"/captures/{cid}/status").status_code != 286:
            assert time.perf_counter() < deadline
            time.sleep(0.02)
        with open_db(settings) as conn:
            assert len(capture_items(conn, cid)) == 12
    assert client.app.state.worker is None and not worker._thread.is_alive()

"""Failure, retry, restart and transient requeue (AC-25, plan 3.6 steps 2 and 7)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai.fake import FakeProvider, FakeStep
from app.ai.provider import ProviderPermanentError, ProviderTransientError
from app.ai.schemas import Usage
from app.capture import service, worker
from app.inventory import locations
from app.main import create_app
from tests.capture_helpers import (
    FIXTURES,
    ai_calls,
    capture_id,
    capture_items,
    capture_row,
    item,
    post_photo,
    post_text,
    result,
    run,
    script_step,
)
from tests.inventory_helpers import HX, open_db

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def no_sleep(seconds: float) -> None:
    return None


@pytest.fixture
def garage(auth_client, auth_settings) -> int:
    with open_db(auth_settings) as conn:
        location_id, _ = locations.create(conn, "Garage")
    return location_id


@pytest.mark.parametrize(
    ("error", "outcome", "message"),
    [
        (ProviderPermanentError("HTTP 400 bad request"), "permanent_error", "HTTP 400 bad request"),
        (RuntimeError("SDK exploded"), "permanent_error", "SDK exploded"),
    ],
)
def test_exception_fails_the_capture_keeping_input_and_retry_succeeds(
    auth_client, auth_settings, garage, error, outcome, message
):
    cid = capture_id(post_photo(auth_client, garage))
    run(auth_settings, FakeProvider([error]))
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        photo = conn.execute("SELECT * FROM photos WHERE capture_id = ?", (cid,)).fetchone()
        calls = ai_calls(conn, cid)
        assert capture_items(conn, cid) == []
    assert capture["status"] == "failed" and capture["error"] == message
    assert capture["provider"] == "fake" and capture["finished_at"] is not None
    assert (auth_settings.data_dir / photo["rel_path"]).is_file()  # input preserved
    assert [c["outcome"] for c in calls] == [outcome]

    status = auth_client.get(f"/captures/{cid}/status")
    assert status.status_code == 286
    assert message in status.text and f'hx-post="/captures/{cid}/retry"' in status.text
    listing = auth_client.get("/captures")
    assert listing.status_code == 200 and f'hx-post="/captures/{cid}/retry"' in listing.text

    retried = auth_client.post(f"/captures/{cid}/retry", headers=HX)
    assert retried.status_code == 200 and 'hx-trigger="every 2s"' in retried.text
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
    assert (capture["status"], capture["attempts"], capture["error"]) == ("queued", 0, None)

    run(auth_settings, FakeProvider([script_step(0)]))
    with open_db(auth_settings) as conn:
        assert capture_row(conn, cid)["status"] == "done"
        assert len(capture_items(conn, cid)) == 12
    assert auth_client.post(f"/captures/{cid}/retry", headers=HX).status_code == 409
    assert auth_client.post("/captures/999/retry", headers=HX).status_code == 404
    assert auth_client.post(f"/captures/{cid}/retry").status_code == 403  # no HX-Request


def test_timeouts_requeue_until_three_attempts_then_fail(auth_client, auth_settings, garage):
    auth_client.app.state.clock = lambda: T0
    cid = capture_id(post_photo(auth_client, garage))
    provider = FakeProvider([FakeStep(timeout=True)] * 3, sleep=no_sleep)
    moment = T0
    for attempt in range(1, 4):
        assert run(auth_settings, provider, now=moment) is True
        with open_db(auth_settings) as conn:
            capture = capture_row(conn, cid)
        assert (capture["status"], capture["attempts"]) == ("queued", attempt)
        assert capture["error"] == "provider call timed out"
        moment += timedelta(seconds=6)
    assert run(auth_settings, provider, now=moment) is True
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        calls = ai_calls(conn, cid)
    assert (capture["status"], capture["error"]) == ("failed", "gave up after 3 attempts")
    assert [c["outcome"] for c in calls] == ["timeout"] * 3
    assert all(c["input_tokens"] == 0 for c in calls)
    assert len(provider.requests) == 3

    auth_client.post(f"/captures/{cid}/retry", headers=HX)
    run(auth_settings, FakeProvider([script_step(0)]), now=moment)
    with open_db(auth_settings) as conn:
        assert capture_row(conn, cid)["status"] == "done"


def test_transient_requeue_waits_for_not_before(auth_client, auth_settings, garage):
    auth_client.app.state.clock = lambda: T0
    cid = capture_id(post_photo(auth_client, garage))
    provider = FakeProvider(
        [ProviderTransientError("HTTP 529 overloaded", usage=Usage(input_tokens=5)), script_step(0)]
    )
    assert run(auth_settings, provider, now=T0) is True
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        calls = ai_calls(conn, cid)
    assert capture["status"] == "queued" and capture["attempts"] == 1
    assert capture["not_before"] == "2026-09-29T12:00:05.000Z"
    assert [(c["outcome"], c["input_tokens"]) for c in calls] == [("transient_error", 5)]
    status = auth_client.get(f"/captures/{cid}/status")
    assert status.status_code == 200 and "Retrying shortly" in status.text

    assert run(auth_settings, provider, now=T0 + timedelta(seconds=4)) is False
    assert len(provider.requests) == 1
    assert run(auth_settings, provider, now=T0 + timedelta(seconds=5)) is True
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
    assert (capture["status"], capture["attempts"]) == ("done", 2)


def test_restart_requeues_processing_captures_keeping_attempts(auth_settings, garage):
    with open_db(auth_settings) as conn:
        cid = service.create_capture(conn, capture_type="text", now=T0, input_text="milk")
        claimed = worker.claim(conn, T0)
        assert claimed["id"] == cid and claimed["status"] == "processing"
    # A new process starts: the lifespan puts it back in the queue.
    with TestClient(create_app(auth_settings)), open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
    assert (capture["status"], capture["attempts"]) == ("queued", 1)
    run(auth_settings, FakeProvider([result([item("Milk")])]), now=T0)
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
    assert (capture["status"], capture["attempts"]) == ("done", 2)


def test_unreadable_upload_fails_without_a_provider_call(auth_client, auth_settings, garage):
    response = auth_client.post(
        "/captures/photo",
        headers=HX,
        data={"location_id": str(garage)},
        files={"photo": ("notes.jpg", b"this is not an image", "image/jpeg")},
    )
    cid = capture_id(response)
    provider = FakeProvider([script_step(0)])
    run(auth_settings, provider)
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        assert ai_calls(conn) == []
    assert capture["status"] == "failed" and capture["error"].startswith("not a readable image")
    assert provider.requests == []


def test_ai_disabled_worker_fails_leftover_captures(auth_client, auth_settings):
    cid = capture_id(post_text(auth_client, "milk"))
    run(auth_settings, None)
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        assert ai_calls(conn) == []
    assert capture["status"] == "failed" and "disabled" in capture["error"]


def test_unexpected_post_claim_error_fails_the_capture_and_retry_works(
    auth_client, auth_settings, garage, monkeypatch
):
    cid = capture_id(post_photo(auth_client, garage, FIXTURES / "iphone.heic", "image/heic"))

    def disk_full(source, dest):
        raise OSError("No space left on device")

    monkeypatch.setattr(worker.images, "make_display_jpeg", disk_full)
    assert run(auth_settings, FakeProvider([script_step(0)])) is True
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
    assert capture["status"] == "failed" and capture["finished_at"] is not None
    assert capture["error"] == "internal error: OSError: No space left on device"

    monkeypatch.undo()
    assert auth_client.post(f"/captures/{cid}/retry", headers=HX).status_code == 200
    run(auth_settings, FakeProvider([script_step(0)]))
    with open_db(auth_settings) as conn:
        assert capture_row(conn, cid)["status"] == "done"


def test_error_after_the_provider_call_keeps_the_ledger_row(
    auth_client, auth_settings, garage, monkeypatch
):
    cid = capture_id(post_photo(auth_client, garage))

    def broken_requeue(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "_requeue", broken_requeue)
    run(auth_settings, FakeProvider([ProviderTransientError("busy")]))
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        calls = ai_calls(conn, cid)
    assert capture["status"] == "failed"
    assert capture["error"] == "internal error: OperationalError: database is locked"
    assert [c["outcome"] for c in calls] == ["transient_error"]

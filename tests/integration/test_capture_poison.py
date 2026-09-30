"""A poison capture stops after 3 attempts instead of crash-looping (AC-25)."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from app.ai.fake import FakeProvider
from app.ai.provider import ProviderTransientError
from app.capture import service, worker
from app.main import create_app
from tests.capture_helpers import ai_calls, capture_row, item, result, run

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def test_input_that_kills_the_worker_fails_after_three_attempts(settings, conn):
    """Each claim is followed by a crash (the capture stays 'processing') and a restart."""
    cid = service.create_capture(conn, capture_type="text", now=T0, input_text="boom")
    for attempt in range(1, 4):
        assert worker.claim(conn, T0)["attempts"] == attempt  # ...and the process dies
        with TestClient(create_app(settings)):  # restart: processing -> queued
            pass
    provider = FakeProvider([result([item("Never")])])
    assert run(settings, provider, now=T0) is True
    capture = capture_row(conn, cid)
    assert ai_calls(conn) == []
    assert (capture["status"], capture["error"], capture["attempts"]) == (
        "failed",
        "gave up after 3 attempts",
        4,
    )
    assert provider.requests == []
    assert run(settings, provider, now=T0) is False


def test_always_transient_input_fails_after_three_calls(settings, conn):
    cid = service.create_capture(conn, capture_type="text", now=T0, input_text="milk")
    provider = FakeProvider([ProviderTransientError("HTTP 503")], repeat_last=True)
    moment = T0
    while run(settings, provider, now=moment):
        moment += timedelta(seconds=5)
    capture = capture_row(conn, cid)
    assert (capture["status"], capture["error"]) == ("failed", "gave up after 3 attempts")
    assert len(provider.requests) == 3
    assert [c["outcome"] for c in ai_calls(conn, cid)] == ["transient_error"] * 3

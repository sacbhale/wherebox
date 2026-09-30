from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.db.timestamps import parse_utc, utc_iso


@pytest.mark.parametrize("value", [None, "", "not a date", "2026-13-01T00:00:00Z"])
def test_parse_utc_rejects_blank_and_invalid(value: str | None) -> None:
    assert parse_utc(value) is None


def test_parse_utc_reads_stored_naive_and_offset_values() -> None:
    expected = datetime(2026, 9, 28, 3, 0, tzinfo=UTC)
    assert parse_utc("2026-09-28T03:00:00.000Z") == expected
    assert parse_utc("2026-09-28T03:00:00") == expected  # naive means UTC
    offset = parse_utc("2026-09-28T08:30:00+05:30")
    assert offset == expected and offset.utcoffset() == timedelta(hours=5, minutes=30)


def test_parse_utc_round_trips_utc_iso() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, 678_000, tzinfo=timezone(timedelta(hours=-7)))
    assert parse_utc(utc_iso(moment)) == moment

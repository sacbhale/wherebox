"""Usage ledger and monthly budget: TZ month windows, cost math, exhaustion (AC-23, AC-24)."""

import sqlite3
from datetime import UTC, datetime

import pytest

from app.ai.schemas import Usage
from app.capture.budget import (
    budget_exhausted,
    cost_micros,
    month_usage,
    month_window,
    record_ai_call,
)
from app.config import Settings
from app.db.timestamps import utc_iso
from tests.conftest import SettingsFactory

PRICED = {"ai_input_price_per_mtok": 3.0, "ai_output_price_per_mtok": 15.0}


def test_utc_iso_matches_sqlite_format(conn: sqlite3.Connection) -> None:
    moment = datetime(2026, 9, 29, 12, 34, 56, 789_123, tzinfo=UTC)
    assert utc_iso(moment) == "2026-09-29T12:34:56.789Z"
    assert utc_iso(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05.000Z"  # naive = UTC
    sqlite_now = conn.execute("SELECT strftime('%Y-%m-%dT%H:%M:%fZ', 'now')").fetchone()[0]
    assert len(sqlite_now) == len(utc_iso(datetime.now(UTC)))


@pytest.mark.parametrize(
    ("tz", "now", "expected"),
    [
        (
            "UTC",
            "2026-09-15T00:00:00+00:00",
            ("2026-09-01T00:00:00.000Z", "2026-10-01T00:00:00.000Z"),
        ),
        # 2026-10-01 02:00 UTC is still Sept 30 22:00 in New York (EDT, UTC-4).
        (
            "America/New_York",
            "2026-10-01T02:00:00+00:00",
            ("2026-09-01T04:00:00.000Z", "2026-10-01T04:00:00.000Z"),
        ),
        # New York: November starts in EDT, December starts in EST (UTC-5).
        (
            "America/New_York",
            "2026-11-15T12:00:00+00:00",
            ("2026-11-01T04:00:00.000Z", "2026-12-01T05:00:00.000Z"),
        ),
        # 2026-09-30 19:00 UTC is already Oct 1 00:30 in Kolkata (UTC+5:30).
        (
            "Asia/Kolkata",
            "2026-09-30T19:00:00+00:00",
            ("2026-09-30T18:30:00.000Z", "2026-10-31T18:30:00.000Z"),
        ),
        # December rolls over to next year.
        (
            "Asia/Kolkata",
            "2026-12-31T20:00:00+00:00",
            ("2026-12-31T18:30:00.000Z", "2027-01-31T18:30:00.000Z"),
        ),
    ],
)
def test_month_window(
    make_settings: SettingsFactory, tz: str, now: str, expected: tuple[str, str]
) -> None:
    assert month_window(make_settings(tz=tz), datetime.fromisoformat(now)) == expected


def test_cost_math(make_settings: SettingsFactory) -> None:
    settings = make_settings(**PRICED)
    # 1M input at $3/MTok = $3 = 3,000,000 micros; 1M output at $15 = 15,000,000.
    assert cost_micros(1_000_000, 0, settings) == 3_000_000
    assert cost_micros(0, 1_000_000, settings) == 15_000_000
    assert cost_micros(1234, 56, settings) == 1234 * 3 + 56 * 15
    assert (
        cost_micros(
            1, 1, make_settings(ai_input_price_per_mtok=0.25, ai_output_price_per_mtok=1.25)
        )
        == 2
    )
    assert cost_micros(1000, 1000, make_settings()) == 0  # prices unset


def _record(
    conn: sqlite3.Connection,
    settings: Settings,
    when: str,
    usage: Usage | None,
    outcome: str = "ok",
) -> None:
    record_ai_call(conn, settings, None, datetime.fromisoformat(when), usage, outcome)


def test_record_ai_call_writes_ledger_row(
    conn: sqlite3.Connection, make_settings: SettingsFactory
) -> None:
    settings = make_settings(**PRICED)
    row_id = record_ai_call(
        conn,
        settings,
        None,
        datetime(2026, 9, 3, 8, 0, tzinfo=UTC),
        Usage(input_tokens=100, output_tokens=10),
        "invalid_output",
    )
    row = conn.execute("SELECT * FROM ai_calls WHERE id = ?", (row_id,)).fetchone()
    assert dict(row) == {
        "id": row_id,
        "capture_id": None,
        "started_at": "2026-09-03T08:00:00.000Z",
        "input_tokens": 100,
        "output_tokens": 10,
        "cost_micros": 450,
        "outcome": "invalid_output",
    }
    assert not conn.in_transaction


def test_record_timeout_with_no_usage(
    conn: sqlite3.Connection, make_settings: SettingsFactory
) -> None:
    settings = make_settings(**PRICED)
    _record(conn, settings, "2026-09-03T08:00:00+00:00", None, "timeout")
    row = conn.execute(
        "SELECT input_tokens, output_tokens, cost_micros, outcome FROM ai_calls"
    ).fetchone()
    assert tuple(row) == (0, 0, 0, "timeout")


def test_record_rejects_unknown_outcome(
    conn: sqlite3.Connection, make_settings: SettingsFactory
) -> None:
    with pytest.raises(ValueError):
        _record(conn, make_settings(), "2026-09-03T08:00:00+00:00", None, "weird")


def test_month_usage_respects_tz_boundaries(
    conn: sqlite3.Connection, make_settings: SettingsFactory
) -> None:
    settings = make_settings(tz="America/New_York", **PRICED)
    u = Usage(input_tokens=1000, output_tokens=100)
    _record(conn, settings, "2026-09-01T03:59:59+00:00", u)  # Aug 31 in New York
    _record(conn, settings, "2026-09-01T04:00:00+00:00", u)  # Sep 1 00:00 New York
    _record(conn, settings, "2026-10-01T03:59:59+00:00", u)  # Sep 30 23:59 New York
    _record(conn, settings, "2026-10-01T04:00:00+00:00", u)  # Oct 1 New York
    usage = month_usage(conn, settings, datetime(2026, 9, 15, tzinfo=UTC))
    assert (usage.input_tokens, usage.output_tokens) == (2000, 200)
    assert usage.cost_micros == 2 * (1000 * 3 + 100 * 15)
    assert usage.cost_usd == pytest.approx(0.009)


def test_month_usage_kolkata(conn: sqlite3.Connection, make_settings: SettingsFactory) -> None:
    settings = make_settings(tz="Asia/Kolkata", **PRICED)
    u = Usage(input_tokens=10, output_tokens=0)
    _record(conn, settings, "2026-09-30T18:29:59+00:00", u)  # Sep 30 23:59 Kolkata
    _record(conn, settings, "2026-09-30T18:30:00+00:00", u)  # Oct 1 00:00 Kolkata
    october = month_usage(conn, settings, datetime(2026, 10, 10, tzinfo=UTC))
    september = month_usage(conn, settings, datetime(2026, 9, 10, tzinfo=UTC))
    assert (october.input_tokens, september.input_tokens) == (10, 10)


def test_empty_month(conn: sqlite3.Connection, make_settings: SettingsFactory) -> None:
    usage = month_usage(conn, make_settings(), datetime(2026, 9, 15, tzinfo=UTC))
    assert (usage.input_tokens, usage.output_tokens, usage.cost_micros) == (0, 0, 0)


def test_budget_exhausted_at_or_above(
    conn: sqlite3.Connection, make_settings: SettingsFactory
) -> None:
    # Budget $0.01 = 10,000 micros. 1000 output tokens at $5/MTok = 5,000 micros each.
    settings = make_settings(
        monthly_ai_budget=0.01, ai_input_price_per_mtok=0, ai_output_price_per_mtok=5
    )
    now = datetime(2026, 9, 15, tzinfo=UTC)
    u = Usage(input_tokens=0, output_tokens=1000)
    assert not budget_exhausted(conn, settings, now)
    _record(conn, settings, "2026-09-02T00:00:00+00:00", u)
    assert not budget_exhausted(conn, settings, now)  # 5,000 < 10,000
    _record(conn, settings, "2026-09-03T00:00:00+00:00", u)
    assert budget_exhausted(conn, settings, now)  # exactly 10,000: at the budget counts
    _record(conn, settings, "2026-09-04T00:00:00+00:00", u)
    assert budget_exhausted(conn, settings, now)
    assert not budget_exhausted(conn, settings, datetime(2026, 10, 2, tzinfo=UTC))  # new month


def test_failed_calls_with_usage_count(
    conn: sqlite3.Connection, make_settings: SettingsFactory
) -> None:
    settings = make_settings(
        monthly_ai_budget=0.001, ai_input_price_per_mtok=1, ai_output_price_per_mtok=1
    )
    _record(conn, settings, "2026-09-02T00:00:00+00:00", Usage(input_tokens=1000), "invalid_output")
    assert budget_exhausted(conn, settings, datetime(2026, 9, 15, tzinfo=UTC))


def test_unset_budget_is_never_exhausted(
    conn: sqlite3.Connection, make_settings: SettingsFactory
) -> None:
    settings = make_settings(**PRICED)
    _record(
        conn, settings, "2026-09-02T00:00:00+00:00", Usage(input_tokens=10**9, output_tokens=10**9)
    )
    assert not budget_exhausted(conn, settings, datetime(2026, 9, 15, tzinfo=UTC))


def test_zero_budget_is_exhausted_immediately(
    conn: sqlite3.Connection, make_settings: SettingsFactory
) -> None:
    settings = make_settings(monthly_ai_budget=0, **PRICED)
    assert budget_exhausted(conn, settings, datetime(2026, 9, 15, tzinfo=UTC))

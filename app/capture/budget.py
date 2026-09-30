"""AI usage ledger (``ai_calls``) and the monthly budget.

Month boundaries are computed in ``settings.tz`` and converted to a UTC range of stored
timestamps (see ``app.db.timestamps``).
"""

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.ai.schemas import Usage
from app.config import Settings
from app.db.connection import write_tx
from app.db.timestamps import utc_iso

OUTCOMES = frozenset({"ok", "invalid_output", "transient_error", "permanent_error", "timeout"})


@dataclass(frozen=True)
class MonthUsage:
    input_tokens: int
    output_tokens: int
    cost_micros: int

    @property
    def cost_usd(self) -> float:
        return self.cost_micros / 1_000_000


def month_window(settings: Settings, now: datetime) -> tuple[str, str]:
    """[start, end) of the calendar month containing ``now`` in settings.tz, as UTC strings."""
    tz = ZoneInfo(settings.tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    local = now.astimezone(tz)
    start = datetime(local.year, local.month, 1, tzinfo=tz)
    if local.month == 12:
        end = datetime(local.year + 1, 1, 1, tzinfo=tz)
    else:
        end = datetime(local.year, local.month + 1, 1, tzinfo=tz)
    return utc_iso(start), utc_iso(end)


def cost_micros(input_tokens: int, output_tokens: int, settings: Settings) -> int:
    """Cost in micro-USD. Prices are USD per million tokens, so tokens * price = micros.

    Unset prices count as 0 (the budget cannot be set without them).
    """
    input_price = settings.ai_input_price_per_mtok or 0.0
    output_price = settings.ai_output_price_per_mtok or 0.0
    return round(input_tokens * input_price + output_tokens * output_price)


def month_usage(conn: sqlite3.Connection, settings: Settings, now: datetime) -> MonthUsage:
    start, end = month_window(settings, now)
    row = conn.execute(
        "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0),"
        " COALESCE(SUM(cost_micros), 0) FROM ai_calls WHERE started_at >= ? AND started_at < ?",
        (start, end),
    ).fetchone()
    return MonthUsage(input_tokens=row[0], output_tokens=row[1], cost_micros=row[2])


def budget_micros(settings: Settings) -> int | None:
    if settings.monthly_ai_budget is None:
        return None
    return round(settings.monthly_ai_budget * 1_000_000)


def budget_exhausted(conn: sqlite3.Connection, settings: Settings, now: datetime) -> bool:
    """True when this month's ledger total is at or above MONTHLY_AI_BUDGET (unset: never)."""
    limit = budget_micros(settings)
    if limit is None:
        return False
    return month_usage(conn, settings, now).cost_micros >= limit


def record_ai_call(
    conn: sqlite3.Connection,
    settings: Settings,
    capture_id: int | None,
    started_at: datetime,
    usage: Usage | None,
    outcome: str,
) -> int:
    """Insert one ledger row in its own write_tx; returns its id. ``usage=None`` records 0."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown ai_calls outcome {outcome!r}")
    usage = usage or Usage()
    with write_tx(conn):
        cursor = conn.execute(
            "INSERT INTO ai_calls"
            " (capture_id, started_at, input_tokens, output_tokens, cost_micros, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                capture_id,
                utc_iso(started_at),
                usage.input_tokens,
                usage.output_tokens,
                cost_micros(usage.input_tokens, usage.output_tokens, settings),
                outcome,
            ),
        )
    assert cursor.lastrowid is not None
    return cursor.lastrowid

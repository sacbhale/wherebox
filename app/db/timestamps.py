"""UTC timestamps as stored in the database.

Stored values use SQLite's own format (``YYYY-MM-DDTHH:MM:SS.fffZ``), so string comparison
orders them correctly and Python-written values match ``strftime(...)`` defaults.
"""

from datetime import UTC, datetime

# SQL expression for "now" in the stored format.
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def utcnow() -> datetime:
    return datetime.now(UTC)


def utc_iso(moment: datetime) -> str:
    """UTC ISO string matching SQLite's strftime('%Y-%m-%dT%H:%M:%fZ'). Naive means UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def parse_utc(value: str | None) -> datetime | None:
    """A stored timestamp as an aware datetime (naive means UTC); None if blank or invalid."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

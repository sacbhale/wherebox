"""Data for the Settings page: AI month totals, budget, photo-dir size, backups, warnings."""

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from app.ai.registry import ai_disabled_reason
from app.capture.budget import MonthUsage, budget_micros, month_usage
from app.capture.service import BUDGET_REACHED
from app.config import Settings
from app.db.timestamps import parse_utc, utcnow

PHOTO_SIZE_CACHE_SECONDS = 600
QR_WARNING = "Changing this orphans every printed QR/NFC tag."
TIMEOUT_NOTE = (
    "Timeouts are not counted: a timed-out call reports no usage, although the provider "
    "may still bill for it."
)

_size_cache: dict[Path, tuple[float, int]] = {}
_size_lock = threading.Lock()


def _tree_size(path: Path) -> int:
    total = 0
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        total += _tree_size(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        return total
    return total


def photo_dir_size(path: Path, *, monotonic: float | None = None) -> int:
    """Total bytes under ``path`` (os.scandir recursion), cached for 10 minutes per path."""
    now = time.monotonic() if monotonic is None else monotonic
    with _size_lock:
        cached = _size_cache.get(path)
        if cached is not None and now - cached[0] < PHOTO_SIZE_CACHE_SECONDS:
            return cached[1]
    size = _tree_size(path)
    with _size_lock:
        _size_cache[path] = (now, size)
    return size


def clear_photo_size_cache() -> None:
    with _size_lock:
        _size_cache.clear()


def human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


def last_backup_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = 'last_backup_at'").fetchone()
    return row[0] if row and row[0] else None


@dataclass(frozen=True)
class SettingsInfo:
    provider: str
    model: str
    ai_disabled_reason: str | None
    usage: MonthUsage
    budget_usd: float | None
    remaining_usd: float | None
    budget_exhausted: bool
    prices_set: bool
    public_base_url: str
    last_backup_at: str | None
    photo_dir_bytes: int
    timezone: str
    warnings: list[str] = field(default_factory=list)

    @property
    def cost_usd(self) -> float:
        return self.usage.cost_usd

    @property
    def photo_dir_human(self) -> str:
        return human_bytes(self.photo_dir_bytes)


def build_settings_info(
    conn: sqlite3.Connection, settings: Settings, now: datetime | None = None
) -> SettingsInfo:
    now = now or utcnow()
    usage = month_usage(conn, settings, now)
    limit = budget_micros(settings)
    exhausted = limit is not None and usage.cost_micros >= limit
    remaining = None if limit is None else max(limit - usage.cost_micros, 0) / 1_000_000
    prices_set = (
        settings.ai_input_price_per_mtok is not None
        and settings.ai_output_price_per_mtok is not None
    )
    backup = last_backup_at(conn)
    disabled = ai_disabled_reason(settings)

    warnings: list[str] = []
    if disabled:
        warnings.append(disabled)
    if exhausted:
        warnings.append(BUDGET_REACHED)
    if not prices_set:
        warnings.append(
            "AI prices are not set (AI_INPUT_PRICE_PER_MTOK / AI_OUTPUT_PRICE_PER_MTOK), "
            "so costs show as $0."
        )
    backup_time = parse_utc(backup)
    if backup_time is None:
        warnings.append("No backup has been written yet.")
    elif now - backup_time > timedelta(hours=2 * settings.backup_interval_hours):
        warnings.append(f"The last backup is older than {2 * settings.backup_interval_hours:g}h.")

    return SettingsInfo(
        provider=settings.ai_provider,
        model=settings.ai_model,
        ai_disabled_reason=disabled,
        usage=usage,
        budget_usd=settings.monthly_ai_budget,
        remaining_usd=remaining,
        budget_exhausted=exhausted,
        prices_set=prices_set,
        public_base_url=settings.public_base_url,
        last_backup_at=backup,
        photo_dir_bytes=photo_dir_size(settings.photos_dir),
        timezone=settings.tz,
        warnings=warnings,
    )

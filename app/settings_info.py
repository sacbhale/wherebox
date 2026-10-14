"""Data for the Settings page: AI month totals, budget, photo-dir size, backups, warnings."""

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from app.ai import openrouter_catalog
from app.ai.model_choice import effective_model, model_selectable, stored_model
from app.ai.openrouter_catalog import Catalog, ModelInfo
from app.ai.registry import ai_disabled_reason
from app.auth.passwords import get_username
from app.capture.budget import (
    PICKER_LIMIT,
    MonthUsage,
    budget_micros,
    month_usage,
    token_estimate,
)
from app.capture.service import BUDGET_REACHED
from app.config import Settings
from app.db.timestamps import parse_utc, utcnow

PHOTO_SIZE_CACHE_SECONDS = 600
QR_WARNING = "Changing this orphans every printed QR/NFC tag."
NO_USERNAME_WARNING = (
    "No login name is set — add HOUSEHOLD_USERNAME to .env (or run `cli set-username`) and restart."
)
TIMEOUT_NOTE = (
    "A timed-out call reports no usage. Anthropic timeouts are recorded as $0, although "
    "they may still be billed; OpenRouter timeouts and dropped calls are charged an "
    "estimate (typical input plus the full output cap at the model's catalog price)."
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
    model = effective_model(conn, settings)
    disabled = ai_disabled_reason(settings, model)

    warnings: list[str] = []
    if get_username(conn) is None:
        warnings.append(NO_USERNAME_WARNING)
    if disabled:
        warnings.append(disabled)
    if exhausted:
        warnings.append(BUDGET_REACHED)
    if not prices_set and settings.ai_provider != "openrouter":
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
        model=model,
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


# ---------------------------------------------------------------- AI model picker


@dataclass(frozen=True)
class ModelRow:
    model: ModelInfo
    capture_usd: float | None  # rough estimated cost of one capture


@dataclass(frozen=True)
class ModelResults:
    rows: list[ModelRow]
    total: int
    query: str
    images_only: bool
    tokens: tuple[int, int, bool]
    catalog: Catalog


def model_results(
    conn: sqlite3.Connection,
    settings: Settings,
    query: str = "",
    images_only: bool = True,
    *,
    routers: bool = True,
) -> ModelResults:
    """Picker rows (at most PICKER_LIMIT) for the query, with a rough cost per capture.
    ``routers=False`` leaves out OpenRouter's routing pseudo-models (comparison picker)."""
    catalog = openrouter_catalog.load_catalog(settings)
    matches = [
        model
        for model in openrouter_catalog.list_models(settings, query, images_only)
        if routers or not model.is_router
    ]
    tokens = token_estimate(conn, 1)  # per 1-photo capture
    rows = [ModelRow(model, model.cost_usd(tokens[0], tokens[1])) for model in matches]
    return ModelResults(rows[:PICKER_LIMIT], len(rows), query, images_only, tokens, catalog)


@dataclass(frozen=True)
class ModelSection:
    """The Settings page "AI model" section (only rendered when ``selectable``)."""

    selectable: bool
    model: str
    env_model: str
    overridden: bool
    current: ModelInfo | None
    catalog: Catalog
    warnings: list[str]
    results: ModelResults | None


def _catalog_age(catalog: Catalog, now: datetime) -> str:
    if catalog.fetched_at is None:
        return "an unknown time"
    minutes = max(int((now - catalog.fetched_at).total_seconds() // 60), 0)
    if minutes < 120:
        return f"{minutes} min"
    hours = minutes // 60
    return f"{hours} h" if hours < 48 else f"{hours // 24} days"


def build_model_section(
    conn: sqlite3.Connection,
    settings: Settings,
    query: str = "",
    images_only: bool = True,
    now: datetime | None = None,
) -> ModelSection:
    selectable = model_selectable(settings)
    model = effective_model(conn, settings)
    overridden = selectable and stored_model(conn) is not None
    if not selectable:
        return ModelSection(False, model, settings.ai_model, False, None, Catalog(), [], None)
    results = model_results(conn, settings, query, images_only)
    catalog = results.catalog
    current = catalog.get(model)
    warnings: list[str] = []
    if not catalog.available:
        warnings.append(
            "The OpenRouter model list is unavailable right now; the current model keeps working."
        )
    elif catalog.source == "cache":
        warnings.append(
            "OpenRouter is unreachable: showing the model list from "
            f"{_catalog_age(catalog, now or utcnow())} ago."
        )
    if catalog.available and catalog.fresh and current is None:
        warnings.append(
            f"Model {model} is no longer available on OpenRouter — pick another model below."
        )
    if current is not None and not current.accepts_images:
        warnings.append(
            f"Model {model} does not accept images: photo, receipt and reconcile captures "
            "will fail. Text captures still work."
        )
    return ModelSection(
        True, model, settings.ai_model, overridden, current, catalog, warnings, results
    )

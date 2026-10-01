"""AI usage ledger (``ai_calls``) and the monthly budget.

Month boundaries are computed in ``settings.tz`` and converted to a UTC range of stored
timestamps (see ``app.db.timestamps``).
"""

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.ai import openrouter_catalog
from app.ai.openrouter_adapter import DEFAULT_MAX_TOKENS
from app.ai.openrouter_catalog import ModelInfo
from app.ai.provider import AIProvider
from app.ai.schemas import ExtractRequest, Usage, output_cap
from app.config import Settings
from app.db.connection import write_tx
from app.db.timestamps import utc_iso

log = logging.getLogger(__name__)

OUTCOMES = frozenset({"ok", "invalid_output", "transient_error", "permanent_error", "timeout"})
# Rough per-capture token estimate when the ledger has no successful call yet.
DEFAULT_CAPTURE_TOKENS = (1_500, 400)
# Image-aware estimate (spec-multiphoto R10): per call, a fixed base (prompt, tool schema,
# answer overhead) plus a per-image share. The defaults add up to DEFAULT_CAPTURE_TOKENS for
# a 1-photo capture; with history, the per-image share is the average of recent calls that
# sent images. Output grows with images too (more views, more boxes).
CALL_BASE_TOKENS = (300, 100)
DEFAULT_IMAGE_TOKENS = (1_200, 300)
ESTIMATE_SAMPLE = 20
# Rows shown by the model pickers (Settings and comparison).
PICKER_LIMIT = 50


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


def capture_token_estimate(conn: sqlite3.Connection) -> tuple[int, int, bool]:
    """(input, output, from_history): the average tokens of the last ESTIMATE_SAMPLE 'ok'
    calls, or DEFAULT_CAPTURE_TOKENS (from_history False) when there are none."""
    row = conn.execute(
        "SELECT count(*), avg(input_tokens), avg(output_tokens) FROM"
        " (SELECT input_tokens, output_tokens FROM ai_calls WHERE outcome = 'ok'"
        " ORDER BY started_at DESC, id DESC LIMIT ?)",
        (ESTIMATE_SAMPLE,),
    ).fetchone()
    if not row[0]:
        return (*DEFAULT_CAPTURE_TOKENS, False)
    return round(row[1]), round(row[2]), True


# Images of a ledger row from before image counts were recorded (image_count NULL): 0 for a
# text capture (or a comparison of one), else 1 (such a call sent one photo, or a PDF
# receipt whose page count was not recorded).
_ROW_IMAGES = (
    "coalesce(a.image_count, CASE WHEN coalesce(c.type, cc.type) = 'text' THEN 0 ELSE 1 END)"
)


def token_estimate(conn: sqlite3.Connection, images: int) -> tuple[int, int, bool]:
    """(input, output, from_history) tokens of one call sending ``images`` images: the call
    base plus ``images`` x the per-image average of the last ESTIMATE_SAMPLE 'ok' calls that
    sent images (DEFAULT_IMAGE_TOKENS without such history). Rows from before image counts
    were recorded (NULL) count as ``_ROW_IMAGES``: 0 for text (left out), else 1. A call
    without images (text) uses ``capture_token_estimate``."""
    if images <= 0:
        return capture_token_estimate(conn)
    base_in, base_out = CALL_BASE_TOKENS
    row = conn.execute(
        "SELECT count(*), avg(max(input_tokens - ?, 0) * 1.0 / images),"
        " avg(max(output_tokens - ?, 0) * 1.0 / images) FROM"
        f" (SELECT a.input_tokens, a.output_tokens, {_ROW_IMAGES} AS images FROM ai_calls a"
        "  LEFT JOIN captures c ON c.id = a.capture_id"
        "  LEFT JOIN comparison_results r ON r.id = a.comparison_result_id"
        "  LEFT JOIN benchmark_cases b ON b.id = r.case_id"
        "  LEFT JOIN captures cc ON cc.id = b.capture_id"
        f"  WHERE a.outcome = 'ok' AND {_ROW_IMAGES} > 0"
        "  ORDER BY a.started_at DESC, a.id DESC LIMIT ?)",
        (base_in, base_out, ESTIMATE_SAMPLE),
    ).fetchone()
    if row[0]:
        per_in, per_out, history = row[1], row[2], True
    else:
        (per_in, per_out), history = DEFAULT_IMAGE_TOKENS, False
    return round(base_in + per_in * images), round(base_out + per_out * images), history


def cost_micros_for(
    usage: Usage,
    settings: Settings,
    model_info: ModelInfo | None = None,
    *,
    provider: str | None = None,
) -> int:
    """Ledger cost of one call in micro-USD: the provider-reported ``usage.cost_usd`` when
    present; else the catalog prices of ``model_info`` (with a warning); else, for any
    provider but OpenRouter, the AI_*_PRICE_PER_MTOK prices (else 0). An OpenRouter call
    never uses those (Anthropic) prices: without a cost or catalog price it records 0 and
    logs an error."""
    if usage.cost_usd is not None:
        return round(usage.cost_usd * 1_000_000)
    if model_info is not None and model_info.price_known:
        estimate = model_info.cost_usd(usage.input_tokens, usage.output_tokens) or 0.0
        if usage.input_tokens or usage.output_tokens:
            log.warning("no reported cost for a %s call; using catalog prices", model_info.id)
        return round(estimate * 1_000_000)
    if provider == "openrouter":
        if usage.input_tokens or usage.output_tokens:
            log.error("OpenRouter call without a reported cost or catalog price: recorded as $0")
        return 0
    return cost_micros(usage.input_tokens, usage.output_tokens, settings)


def ledger_usage(
    conn: sqlite3.Connection,
    settings: Settings,
    provider: AIProvider,
    usage: Usage | None,
    error: BaseException | None,
    request: ExtractRequest | None = None,
) -> tuple[Usage | None, ModelInfo | None]:
    """What to record for one finished call: (usage, catalog entry for the cost fallback).

    Only OpenRouter calls change: without a reported cost the model's catalog entry is looked
    up; a call that may have been billed but reported no usage (timeout or dropped connection
    after the request was sent, oversized response) is recorded at a conservative estimate:
    the input estimate for ``request``'s image count (``token_estimate``) plus the request's
    full output cap (``max_tokens``, larger for photo modes), at catalog prices.
    Never raises (a ledger row must always be written)."""
    if provider.name != "openrouter" or (usage is not None and usage.cost_usd is not None):
        return usage, None
    try:
        info = openrouter_catalog.get_model(settings, provider.model)
    except Exception:
        log.exception("catalog lookup for %s failed", provider.model)
        info = None
    if getattr(error, "maybe_billed", False) and (usage is None or usage == Usage()):
        images = len(request.images) if request is not None else 0
        try:
            input_tokens = token_estimate(conn, images)[0]
        except sqlite3.Error:
            log.exception("token estimate failed")
            input_tokens = DEFAULT_CAPTURE_TOKENS[0]
        output_tokens = getattr(provider, "max_tokens", DEFAULT_MAX_TOKENS)
        if request is not None:
            output_tokens = output_cap(request.mode, output_tokens)
        cost = info.cost_usd(input_tokens, output_tokens) if info is not None else None
        if cost is None:
            log.error(
                "no catalog price for %s: a possibly billed call is recorded as $0", provider.model
            )
        usage = Usage(input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost)
    return usage, info


def insert_ai_call(
    conn: sqlite3.Connection,
    settings: Settings,
    capture_id: int | None,
    started_at: datetime,
    usage: Usage | None,
    outcome: str,
    *,
    comparison_result_id: int | None = None,
    model_info: ModelInfo | None = None,
    provider: str | None = None,
    image_count: int | None = None,
) -> int:
    """Insert one ledger row inside the caller's write_tx; returns its id. ``usage=None``
    records 0. ``model_info`` and ``provider`` feed ``cost_micros_for``; ``image_count`` is
    how many images the call sent (image-aware estimates)."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown ai_calls outcome {outcome!r}")
    usage = usage or Usage()
    cursor = conn.execute(
        "INSERT INTO ai_calls (capture_id, comparison_result_id, started_at, input_tokens,"
        " output_tokens, cost_micros, outcome, image_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            capture_id,
            comparison_result_id,
            utc_iso(started_at),
            usage.input_tokens,
            usage.output_tokens,
            cost_micros_for(usage, settings, model_info, provider=provider),
            outcome,
            image_count,
        ),
    )
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def record_ai_call(
    conn: sqlite3.Connection,
    settings: Settings,
    capture_id: int | None,
    started_at: datetime,
    usage: Usage | None,
    outcome: str,
    *,
    comparison_result_id: int | None = None,
    model_info: ModelInfo | None = None,
    provider: str | None = None,
    image_count: int | None = None,
) -> int:
    """Insert one ledger row in its own write_tx; returns its id. ``usage=None`` records 0."""
    with write_tx(conn):
        return insert_ai_call(
            conn,
            settings,
            capture_id,
            started_at,
            usage,
            outcome,
            comparison_result_id=comparison_result_id,
            model_info=model_info,
            provider=provider,
            image_count=image_count,
        )

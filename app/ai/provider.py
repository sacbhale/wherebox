"""The AI provider contract. See docs/ai-provider-contract.md.

Capture code depends only on this module, ``schemas`` and ``registry``; SDKs stay inside
their adapter module.
"""

import math
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Protocol

from app.ai.schemas import ExtractRequest, ExtractResult, Usage

# A Retry-After above this is read as this (a bogus header must not stall a queue forever).
MAX_RETRY_AFTER_S = 3600.0
# Provider error text kept in messages, logs and the database (whitespace collapsed).
MAX_ERROR_CHARS = 500
_WHITESPACE = re.compile(r"\s+")


def bounded(text: str, limit: int = MAX_ERROR_CHARS) -> str:
    """``text`` on one line (runs of whitespace become one space), at most ``limit`` chars."""
    text = _WHITESPACE.sub(" ", text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """A ``Retry-After`` header in seconds (delta-seconds or an HTTP date), or None when it
    is missing, unreadable or not finite. A date in the past is 0; anything longer than
    MAX_RETRY_AFTER_S is MAX_RETRY_AFTER_S."""
    if not value or not value.strip():
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - (now or datetime.now(UTC))).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_S)


class AIProvider(Protocol):
    name: str
    model: str

    def extract(self, req: ExtractRequest) -> ExtractResult:
        """Synchronous; called from the worker thread only. Raises a ProviderError on failure."""
        ...


class ProviderError(Exception):
    """Base class. ``usage`` is what the provider billed for this call, if it reported any.
    ``maybe_billed`` marks a call that reached the provider but ended without a usage report
    (e.g. a read timeout): the provider may still bill it, so the ledger records an estimate
    (``budget.ledger_usage``). ``detail`` is the provider's own (redacted, bounded) message
    when ``message`` is a plain-language one instead."""

    def __init__(
        self,
        message: str = "",
        usage: Usage | None = None,
        *,
        maybe_billed: bool = False,
        detail: str | None = None,
    ) -> None:
        super().__init__(message or type(self).__name__)
        self.usage = usage
        self.maybe_billed = maybe_billed
        self.detail = detail


class ProviderTransientError(ProviderError):
    """Worth retrying: timeouts, connection errors, 429, 5xx, overloaded. ``retry_after``
    is the provider's ``Retry-After`` in seconds (None when it sent none)."""

    def __init__(
        self,
        message: str = "",
        usage: Usage | None = None,
        *,
        maybe_billed: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, usage, maybe_billed=maybe_billed)
        self.retry_after = retry_after


def capped_retry_after(error: ProviderTransientError, cap: timedelta) -> timedelta:
    """The error's ``retry_after`` as a timedelta, from 0 up to ``cap`` (None, NaN or a
    negative value is 0), so no adapter value can overflow a timedelta."""
    asked = error.retry_after
    if asked is None or math.isnan(asked) or asked <= 0:
        return timedelta()
    return timedelta(seconds=min(asked, cap.total_seconds()))


class ProviderRateLimited(ProviderTransientError):
    """HTTP 429: the model's provider is busy (comparisons retry these more patiently)."""


class ProviderTimeoutError(ProviderTransientError):
    """The call exceeded ``ExtractRequest.timeout_s``. Usage is never known (ledger: 'timeout')."""

    def __init__(
        self, message: str = "provider call timed out", *, maybe_billed: bool = False
    ) -> None:
        super().__init__(message, usage=None, maybe_billed=maybe_billed)


class ProviderPermanentError(ProviderError):
    """Retrying will not help: bad request, auth, permission, not found, other 4xx."""


class ProviderInvalidOutput(ProviderError):
    """The call succeeded (and was billed) but its output failed validation."""

    usage: Usage

    def __init__(self, message: str, usage: Usage, raw: str, *, maybe_billed: bool = False) -> None:
        super().__init__(message, usage=usage, maybe_billed=maybe_billed)
        self.raw = raw


def ledger_outcome(exc: BaseException | None) -> str:
    """Map a call's outcome to the ``ai_calls.outcome`` value."""
    if exc is None:
        return "ok"
    if isinstance(exc, ProviderTimeoutError):
        return "timeout"
    if isinstance(exc, ProviderTransientError):
        return "transient_error"
    if isinstance(exc, ProviderInvalidOutput):
        return "invalid_output"
    return "permanent_error"

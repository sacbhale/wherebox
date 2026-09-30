"""The AI provider contract. See docs/ai-provider-contract.md.

Capture code depends only on this module, ``schemas`` and ``registry``; SDKs stay inside
their adapter module.
"""

from typing import Protocol

from app.ai.schemas import ExtractRequest, ExtractResult, Usage


class AIProvider(Protocol):
    name: str
    model: str

    def extract(self, req: ExtractRequest) -> ExtractResult:
        """Synchronous; called from the worker thread only. Raises a ProviderError on failure."""
        ...


class ProviderError(Exception):
    """Base class. ``usage`` is what the provider billed for this call, if it reported any."""

    def __init__(self, message: str = "", usage: Usage | None = None) -> None:
        super().__init__(message or type(self).__name__)
        self.usage = usage


class ProviderTransientError(ProviderError):
    """Worth retrying: timeouts, connection errors, 429, 5xx, overloaded."""


class ProviderTimeoutError(ProviderTransientError):
    """The call exceeded ``ExtractRequest.timeout_s``. Usage is never known (ledger: 'timeout')."""

    def __init__(self, message: str = "provider call timed out") -> None:
        super().__init__(message, usage=None)


class ProviderPermanentError(ProviderError):
    """Retrying will not help: bad request, auth, permission, not found, other 4xx."""


class ProviderInvalidOutput(ProviderError):
    """The call succeeded (and was billed) but its output failed validation."""

    usage: Usage

    def __init__(self, message: str, usage: Usage, raw: str) -> None:
        super().__init__(message, usage=usage)
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

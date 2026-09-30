"""Provider selection. Capture code calls ``get_provider(settings)`` and nothing else.

``get_provider`` returns ``None`` when AI is disabled (anthropic without AI_API_KEY);
``ai_disabled_reason`` returns the user-facing message for that case. Callers must check
for ``None`` and offer manual entry instead of AI capture.
"""

from collections.abc import Callable

from app.ai.provider import AIProvider
from app.config import Settings


class UnknownProviderError(ValueError):
    pass


AI_DISABLED_NO_KEY = "AI capture is disabled: set AI_API_KEY to enable it. Manual entry works."


def _anthropic(settings: Settings) -> AIProvider | None:
    if settings.ai_api_key is None:
        return None
    from app.ai.anthropic_adapter import AnthropicProvider  # SDK import stays lazy

    return AnthropicProvider(settings.ai_api_key.get_secret_value(), settings.ai_model)


def _fake(settings: Settings) -> AIProvider:
    from app.ai.fake import FakeProvider, load_script

    if settings.fake_ai_script is not None:
        return load_script(settings.fake_ai_script)
    return FakeProvider()


# Adding a provider: one adapter module plus one entry here (and its name in
# Settings.ai_provider). A factory returns None when the provider is not configured.
PROVIDERS: dict[str, Callable[[Settings], AIProvider | None]] = {
    "anthropic": _anthropic,
    "fake": _fake,
}


def get_provider(settings: Settings) -> AIProvider | None:
    """The configured provider, or None when AI is disabled. Unknown names raise."""
    try:
        factory = PROVIDERS[settings.ai_provider]
    except KeyError:
        raise UnknownProviderError(f"unknown AI_PROVIDER {settings.ai_provider!r}") from None
    return factory(settings)


def ai_disabled_reason(settings: Settings) -> str | None:
    """Why AI capture is off, or None when it is available. Does not build a provider."""
    if settings.ai_provider == "anthropic" and settings.ai_api_key is None:
        return AI_DISABLED_NO_KEY
    return None

"""Provider selection. Capture code calls ``get_provider(settings, model)`` (or the memoized
``make_provider_for(settings)``) and nothing else.

``get_provider`` returns ``None`` when AI is disabled (no API key for the provider, or an
invalid OpenRouter model id); ``ai_disabled_reason`` returns the user-facing message for that
case. Callers must check for ``None`` and offer manual entry instead of AI capture.
"""

import threading
from collections.abc import Callable

from app.ai import openrouter_catalog
from app.ai.fake import FakeProvider, load_script
from app.ai.provider import AIProvider
from app.config import Settings


class UnknownProviderError(ValueError):
    pass


AI_DISABLED_NO_KEY = "AI capture is disabled: set AI_API_KEY to enable it. Manual entry works."
AI_DISABLED_NO_OPENROUTER_KEY = (
    "AI capture is disabled: set OPENROUTER_API_KEY to enable it. Manual entry works."
)


def invalid_model_reason(model: str) -> str:
    return (
        f"AI capture is disabled: {model!r} is not an OpenRouter model id (vendor/model, "
        "e.g. google/gemini-3.1-flash-lite). Set AI_MODEL or pick a model in Settings."
    )


def _anthropic(settings: Settings, model: str | None) -> AIProvider | None:
    if settings.ai_api_key is None:
        return None
    from app.ai.anthropic_adapter import AnthropicProvider  # SDK import stays lazy

    return AnthropicProvider(settings.ai_api_key.get_secret_value(), model or settings.ai_model)


def openrouter_provider(settings: Settings, model: str | None = None) -> AIProvider | None:
    """An OpenRouter provider for ``model`` (default AI_MODEL) whatever AI_PROVIDER is, or
    None without OPENROUTER_API_KEY or with an invalid model id. Its capabilities (output
    strategy, image support) come from the catalog at call time."""
    model = model or settings.ai_model
    if settings.openrouter_api_key is None or not openrouter_catalog.valid_model_id(model):
        return None
    from app.ai.openrouter_adapter import OpenRouterProvider

    return OpenRouterProvider(
        settings.openrouter_api_key.get_secret_value(),
        model,
        model_info=lambda: openrouter_catalog.get_model(settings, model),
    )


def _fake_base(settings: Settings) -> FakeProvider:
    if settings.fake_ai_script is not None:
        return load_script(settings.fake_ai_script)
    return FakeProvider()


def _fake(settings: Settings, model: str | None) -> AIProvider:
    provider = _fake_base(settings)
    return provider if model is None else provider.with_model(model)


# Adding a provider: one adapter module plus one entry here (and its name in
# Settings.ai_provider). A factory takes (settings, model or None for AI_MODEL) and returns
# None when the provider is not configured.
PROVIDERS: dict[str, Callable[[Settings, str | None], AIProvider | None]] = {
    "anthropic": _anthropic,
    "openrouter": openrouter_provider,
    "fake": _fake,
}


def get_provider(settings: Settings, model: str | None = None) -> AIProvider | None:
    """The configured provider for ``model`` (default AI_MODEL; the fake keeps its script's
    model when None), or None when AI is disabled. Unknown names raise."""
    try:
        factory = PROVIDERS[settings.ai_provider]
    except KeyError:
        raise UnknownProviderError(f"unknown AI_PROVIDER {settings.ai_provider!r}") from None
    return factory(settings, model)


def make_provider_for(
    settings: Settings, provider_name: str | None = None
) -> Callable[[str], AIProvider | None]:
    """A thread-safe ``provider_for(model_id)`` memoized per model id, for the worker.

    ``provider_name`` defaults to AI_PROVIDER. The fake provider is built once and every
    model gets a view of it, so a FAKE_AI_SCRIPT is consumed across captures (and model
    changes) exactly as with a single provider.
    """
    name = provider_name or settings.ai_provider
    if name not in PROVIDERS:
        raise UnknownProviderError(f"unknown AI provider {name!r}")
    cache: dict[str, AIProvider | None] = {}
    lock = threading.Lock()
    fake_base: list[FakeProvider] = []

    def provider_for(model: str) -> AIProvider | None:
        with lock:
            if model not in cache:
                if name == "fake":
                    if not fake_base:
                        fake_base.append(_fake_base(settings))
                    cache[model] = fake_base[0].with_model(model)
                else:
                    cache[model] = PROVIDERS[name](settings, model)
            return cache[model]

    return provider_for


def ai_disabled_reason(settings: Settings, model: str | None = None) -> str | None:
    """Why AI capture is off for ``model`` (default AI_MODEL), or None when it is available.
    Does not build a provider."""
    if settings.ai_provider == "anthropic" and settings.ai_api_key is None:
        return AI_DISABLED_NO_KEY
    if settings.ai_provider == "openrouter":
        if settings.openrouter_api_key is None:
            return AI_DISABLED_NO_OPENROUTER_KEY
        model = model or settings.ai_model
        if not openrouter_catalog.valid_model_id(model):
            return invalid_model_reason(model)
    return None

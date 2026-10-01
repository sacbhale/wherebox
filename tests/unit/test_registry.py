"""Registry: the OpenRouter factory, model overrides, the memoized provider_for, and
ai_disabled_reason."""

import json
from pathlib import Path

import pytest

from app.ai import registry
from app.ai.fake import FakeProvider
from app.ai.openrouter_adapter import OpenRouterProvider
from app.ai.schemas import ExtractRequest
from tests.conftest import SettingsFactory

CATALOG = Path(__file__).parent.parent / "fixtures" / "openrouter" / "models.json"


def test_openrouter_factory(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        ai_provider="openrouter",
        openrouter_api_key="sk-or-test",
        ai_model="google/gemini-3.1-flash-lite",
        openrouter_catalog_file=CATALOG,
    )
    provider = registry.get_provider(settings)
    assert isinstance(provider, OpenRouterProvider)
    assert provider.name == "openrouter" and provider.model == "google/gemini-3.1-flash-lite"
    other = registry.get_provider(settings, "openai/gpt-5-nano")
    assert other is not None and other.model == "openai/gpt-5-nano"
    assert registry.ai_disabled_reason(settings) is None
    assert registry.ai_disabled_reason(settings, "openai/gpt-5-nano") is None


def test_openrouter_without_key_is_disabled(make_settings: SettingsFactory) -> None:
    settings = make_settings(ai_provider="openrouter", ai_model="google/gemini-3.1-flash-lite")
    assert registry.get_provider(settings) is None
    assert registry.ai_disabled_reason(settings) == registry.AI_DISABLED_NO_OPENROUTER_KEY
    assert "OPENROUTER_API_KEY" in registry.AI_DISABLED_NO_OPENROUTER_KEY


def test_openrouter_with_invalid_model_is_disabled(make_settings: SettingsFactory) -> None:
    # The Anthropic default AI_MODEL is not an OpenRouter id.
    settings = make_settings(ai_provider="openrouter", openrouter_api_key="sk-or-test")
    assert registry.get_provider(settings) is None
    reason = registry.ai_disabled_reason(settings)
    assert reason is not None and "'claude-sonnet-5-5' is not an OpenRouter model id" in reason
    assert "sk-or-test" not in reason
    assert registry.ai_disabled_reason(settings, "google/gemini-3.1-flash-lite") is None
    assert registry.get_provider(settings, "google/gemini-3.1-flash-lite") is not None


def test_openrouter_provider_works_under_other_providers(make_settings: SettingsFactory) -> None:
    """Comparisons use the OpenRouter factory whatever AI_PROVIDER is (R3)."""
    anthropic = make_settings(ai_provider="anthropic", openrouter_api_key="sk-or-test")
    assert isinstance(registry.openrouter_provider(anthropic, "x/y"), OpenRouterProvider)
    assert registry.openrouter_provider(make_settings(ai_provider="anthropic"), "x/y") is None
    assert registry.openrouter_provider(anthropic, "not-an-id") is None


def test_openrouter_capabilities_come_from_the_catalog(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        ai_provider="openrouter", openrouter_api_key="k", openrouter_catalog_file=CATALOG
    )
    provider = registry.get_provider(settings, "z-ai/glm-5.3-prime")
    assert isinstance(provider, OpenRouterProvider)
    info = provider._model_info()
    assert info is not None and not info.accepts_images


def test_anthropic_model_override(make_settings: SettingsFactory) -> None:
    settings = make_settings(ai_provider="anthropic", ai_api_key="sk-x", ai_model="claude-a")
    assert registry.get_provider(settings).model == "claude-a"
    assert registry.get_provider(settings, "claude-b").model == "claude-b"


def test_fake_model_override_and_script_model(
    make_settings: SettingsFactory, tmp_path: Path
) -> None:
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"model": "scripted", "steps": []}))
    settings = make_settings(ai_provider="fake", fake_ai_script=path)
    assert registry.get_provider(settings).model == "scripted"
    assert registry.get_provider(settings, "test/model-b").model == "test/model-b"


def test_make_provider_for_memoizes_per_model(make_settings: SettingsFactory) -> None:
    settings = make_settings(ai_provider="anthropic", ai_api_key="sk-x")
    provider_for = registry.make_provider_for(settings)
    first = provider_for("claude-a")
    assert provider_for("claude-a") is first
    assert provider_for("claude-b") is not first
    assert provider_for("claude-b").model == "claude-b"


def test_make_provider_for_none_when_disabled(make_settings: SettingsFactory) -> None:
    provider_for = registry.make_provider_for(make_settings(ai_provider="openrouter"))
    assert provider_for("x/y") is None


def test_make_provider_for_fake_shares_one_script(
    make_settings: SettingsFactory, tmp_path: Path
) -> None:
    step = {"result": {"items": []}}
    path = tmp_path / "script.json"
    path.write_text(json.dumps({"steps": [step, step, step]}))
    provider_for = registry.make_provider_for(make_settings(fake_ai_script=path))
    a, b = provider_for("test/model-a"), provider_for("test/model-b")
    assert isinstance(a, FakeProvider) and isinstance(b, FakeProvider)
    assert (a.model, b.model) == ("test/model-a", "test/model-b")
    assert a.remaining == 3
    a.extract(ExtractRequest(mode="text", text="x"))
    b.extract(ExtractRequest(mode="text", text="y"))
    assert a.remaining == b.remaining == 1  # one queue, consumed across models
    assert len(a.requests) == 2


def test_make_provider_for_other_provider_name(make_settings: SettingsFactory) -> None:
    settings = make_settings(ai_provider="fake", openrouter_api_key="sk-or-test")
    provider_for = registry.make_provider_for(settings, "openrouter")
    assert isinstance(provider_for("x/y"), OpenRouterProvider)
    with pytest.raises(registry.UnknownProviderError):
        registry.make_provider_for(settings, "ollama")


def test_disabled_reason_fake_and_anthropic(make_settings: SettingsFactory) -> None:
    assert registry.ai_disabled_reason(make_settings(ai_provider="fake"), "anything") is None
    anthropic = make_settings(ai_provider="anthropic")
    assert registry.ai_disabled_reason(anthropic) == registry.AI_DISABLED_NO_KEY

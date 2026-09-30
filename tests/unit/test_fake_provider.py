"""FakeProvider: scripted queue, errors with/without usage, delays, timeouts, JSON scripts."""

import json
from pathlib import Path

import pytest

from app.ai import registry
from app.ai.fake import FakeProvider, FakeStep, load_script
from app.ai.provider import (
    ProviderInvalidOutput,
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from app.ai.schemas import ExtractedItem, ExtractRequest, ExtractResult, Usage
from tests.conftest import SettingsFactory

ITEM = ExtractedItem(name="Milk", quantity=2, kind="consumable", confidence=0.9)


def _result(tokens: int = 10) -> ExtractResult:
    return ExtractResult(items=[ITEM], usage=Usage(input_tokens=tokens, output_tokens=1))


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def test_queue_order_and_request_recording() -> None:
    fake = FakeProvider([_result(1), _result(2)])
    first = ExtractRequest(mode="shelf", images=[b"a"])
    second = ExtractRequest(mode="text", text="milk")
    assert fake.extract(first).usage.input_tokens == 1
    assert fake.extract(second).usage.input_tokens == 2
    assert fake.requests == [first, second]
    assert fake.remaining == 0


def test_exhausted_script_is_permanent_error() -> None:
    fake = FakeProvider([_result()])
    fake.extract(ExtractRequest(mode="shelf"))
    with pytest.raises(ProviderPermanentError, match="exhausted"):
        fake.extract(ExtractRequest(mode="shelf"))
    assert len(fake.requests) == 2


def test_repeat_last() -> None:
    fake = FakeProvider([_result(7)], repeat_last=True)
    for _ in range(3):
        assert fake.extract(ExtractRequest(mode="shelf")).usage.input_tokens == 7


def test_results_are_copies() -> None:
    fake = FakeProvider([_result()], repeat_last=True)
    fake.extract(ExtractRequest(mode="shelf")).items.clear()
    assert fake.extract(ExtractRequest(mode="shelf")).items == [ITEM]


@pytest.mark.parametrize(
    "error",
    [
        ProviderTransientError("busy"),
        ProviderTransientError("busy", usage=Usage(input_tokens=5)),
        ProviderPermanentError("bad"),
        ProviderPermanentError("bad", usage=Usage(input_tokens=3)),
        ProviderInvalidOutput("junk", Usage(input_tokens=9, output_tokens=2), raw="{}"),
    ],
)
def test_scripted_errors_are_raised_with_their_usage(error: Exception) -> None:
    fake = FakeProvider([error])
    with pytest.raises(type(error)) as info:
        fake.extract(ExtractRequest(mode="shelf"))
    assert info.value is error


def test_delay_and_default_delay() -> None:
    sleeps = Sleeps()
    fake = FakeProvider([FakeStep(_result(), delay_s=0.2), _result()], delay_s=0.05, sleep=sleeps)
    fake.extract(ExtractRequest(mode="shelf", timeout_s=1))
    fake.extract(ExtractRequest(mode="shelf", timeout_s=1))
    assert sleeps.calls == [0.2, 0.05]


def test_timeout_step_and_delay_past_timeout() -> None:
    sleeps = Sleeps()
    fake = FakeProvider([FakeStep(timeout=True), FakeStep(_result(), delay_s=5)], sleep=sleeps)
    for _ in range(2):
        with pytest.raises(ProviderTimeoutError) as info:
            fake.extract(ExtractRequest(mode="shelf", timeout_s=2))
        assert info.value.usage is None
    assert all(seconds > 2 for seconds in sleeps.calls)
    assert len(sleeps.calls) == 2


def test_real_sleep_past_timeout() -> None:
    fake = FakeProvider([FakeStep(timeout=True)])
    with pytest.raises(ProviderTimeoutError):
        fake.extract(ExtractRequest(mode="shelf", timeout_s=0.01))


SCRIPT = {
    "model": "fake-vision",
    "repeat_last": False,
    "steps": [
        {
            "result": {
                "items": [
                    {"name": "Milk", "quantity": 2, "kind": "consumable", "confidence": 0.95},
                ],
                "receipt": {"merchant": "Corner Grocery", "purchase_date": "2026-09-14"},
                "usage": {"input_tokens": 1200, "output_tokens": 150},
            }
        },
        {"delay_s": 0.25, "result": {"items": []}},
        {"error": "transient", "message": "HTTP 529", "usage": None},
        {"error": "permanent", "usage": {"input_tokens": 10, "output_tokens": 0}},
        {
            "error": "invalid_output",
            "usage": {"input_tokens": 900, "output_tokens": 40},
            "raw": "x",
        },
        {"error": "timeout"},
    ],
}


def test_load_script_json(tmp_path: Path) -> None:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(SCRIPT))
    sleeps = Sleeps()
    fake = load_script(path, sleep=sleeps)
    assert fake.model == "fake-vision"
    req = ExtractRequest(mode="receipt", timeout_s=1)

    first = fake.extract(req)
    assert first.receipt is not None and first.receipt.merchant == "Corner Grocery"
    assert first.usage == Usage(input_tokens=1200, output_tokens=150)
    assert fake.extract(req).usage == Usage()  # usage defaults to 0/0
    assert sleeps.calls == [0.25]
    with pytest.raises(ProviderTransientError) as transient:
        fake.extract(req)
    assert transient.value.usage is None
    with pytest.raises(ProviderPermanentError) as permanent:
        fake.extract(req)
    assert permanent.value.usage == Usage(input_tokens=10)
    with pytest.raises(ProviderInvalidOutput) as invalid:
        fake.extract(req)
    assert invalid.value.usage == Usage(input_tokens=900, output_tokens=40)
    assert invalid.value.raw == "x"
    with pytest.raises(ProviderTimeoutError):
        fake.extract(req)


@pytest.mark.parametrize(
    "bad",
    [[], {"steps": "nope"}, {"steps": [{"delay_s": 1}]}, {"steps": [{"error": "weird"}]}],
)
def test_load_script_rejects_malformed(tmp_path: Path, bad: object) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        load_script(path)


def test_registry_builds_fake_from_env_script(
    tmp_path: Path, make_settings: SettingsFactory
) -> None:
    path = tmp_path / "script.json"
    path.write_text(json.dumps(SCRIPT))
    provider = registry.get_provider(make_settings(ai_provider="fake", fake_ai_script=path))
    assert isinstance(provider, FakeProvider)
    assert provider.model == "fake-vision" and provider.remaining == len(SCRIPT["steps"])


def test_registry_fake_without_script(make_settings: SettingsFactory) -> None:
    provider = registry.get_provider(make_settings(ai_provider="fake"))
    assert isinstance(provider, FakeProvider) and provider.remaining == 0
    assert registry.ai_disabled_reason(make_settings(ai_provider="fake")) is None


def test_registry_anthropic(make_settings: SettingsFactory) -> None:
    from app.ai.anthropic_adapter import AnthropicProvider

    no_key = make_settings(ai_provider="anthropic")
    assert registry.get_provider(no_key) is None
    assert registry.ai_disabled_reason(no_key) == registry.AI_DISABLED_NO_KEY
    keyed = make_settings(ai_provider="anthropic", ai_api_key="sk-x", ai_model="claude-test")
    provider = registry.get_provider(keyed)
    assert isinstance(provider, AnthropicProvider) and provider.model == "claude-test"
    assert registry.ai_disabled_reason(keyed) is None


def test_registry_unknown_provider(make_settings: SettingsFactory) -> None:
    settings = make_settings().model_copy(update={"ai_provider": "ollama"})
    with pytest.raises(registry.UnknownProviderError):
        registry.get_provider(settings)

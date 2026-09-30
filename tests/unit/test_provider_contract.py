"""Shared contract tests: every provider behaves the same for the same scenario (AC-26).

Each scenario is set up per provider: FakeProvider is scripted with the equivalent step, the
Anthropic adapter answers from a recorded response over httpx MockTransport (no network).
"""

from collections.abc import Callable
from typing import Any

import pytest

from app.ai.fake import FakeProvider, FakeStep
from app.ai.provider import (
    AIProvider,
    ProviderInvalidOutput,
    ProviderPermanentError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from app.ai.schemas import (
    TOOL_INPUT_SCHEMA,
    ExtractedItem,
    ExtractRequest,
    ExtractResult,
    ToolOutput,
    Usage,
)
from tests.unit.anthropic_mock import build_mock_anthropic, recorded, reply_json, reply_timeout

SCENARIOS = ["success", "invalid_output", "rate_limited", "server_error", "bad_request", "timeout"]

SUCCESS = recorded("success")
SUCCESS_USAGE = Usage(input_tokens=1234, output_tokens=56)
INVALID_USAGE = Usage(input_tokens=900, output_tokens=40)


def _fake(scenario: str) -> FakeProvider:
    step: Any
    if scenario == "success":
        tool_input = SUCCESS["content"][0]["input"]
        step = ExtractResult(
            items=[ExtractedItem.model_validate(item) for item in tool_input["items"]],
            receipt=None,
            usage=SUCCESS_USAGE,
        )
    elif scenario == "invalid_output":
        step = ProviderInvalidOutput("bad", INVALID_USAGE, raw='{"items": 5}')
    elif scenario in ("rate_limited", "server_error"):
        step = ProviderTransientError("HTTP 429")
    elif scenario == "bad_request":
        step = ProviderPermanentError("HTTP 400")
    else:
        step = FakeStep(timeout=True)
    return FakeProvider([step], sleep=lambda _s: None)


_ANTHROPIC_REPLIES = {
    "success": reply_json(200, "success"),
    "invalid_output": reply_json(200, "invalid_tool_output"),
    "rate_limited": reply_json(429, "error_429"),
    "server_error": reply_json(500, "error_500"),
    "bad_request": reply_json(400, "error_400"),
    "timeout": reply_timeout,
}


@pytest.fixture(params=["fake", "anthropic"])
def make_provider(request: pytest.FixtureRequest) -> Callable[[str], AIProvider]:
    if request.param == "fake":
        return _fake
    return lambda scenario: build_mock_anthropic(_ANTHROPIC_REPLIES[scenario]).provider


def _request() -> ExtractRequest:
    return ExtractRequest(mode="shelf", images=[b"\xff\xd8\xff\xd9"], timeout_s=0.5)


def test_identity(make_provider: Callable[[str], AIProvider]) -> None:
    provider = make_provider("success")
    assert isinstance(provider.name, str) and provider.name
    assert isinstance(provider.model, str) and provider.model


def test_success_returns_validated_items_and_usage(
    make_provider: Callable[[str], AIProvider],
) -> None:
    result = make_provider("success").extract(_request())
    assert isinstance(result, ExtractResult)
    assert [(i.name, i.quantity, i.kind) for i in result.items] == [
        ("Pasta", 3, "consumable"),
        ("Duct tape", 1, "belonging"),
    ]
    assert all(0 <= item.confidence <= 1 for item in result.items)
    assert result.usage == SUCCESS_USAGE


def test_invalid_output_carries_usage_and_raw(make_provider: Callable[[str], AIProvider]) -> None:
    with pytest.raises(ProviderInvalidOutput) as info:
        make_provider("invalid_output").extract(_request())
    assert info.value.usage == INVALID_USAGE
    assert info.value.raw


@pytest.mark.parametrize("scenario", ["rate_limited", "server_error"])
def test_retryable_failures_are_transient(
    make_provider: Callable[[str], AIProvider], scenario: str
) -> None:
    with pytest.raises(ProviderTransientError) as info:
        make_provider(scenario).extract(_request())
    assert not isinstance(info.value, ProviderTimeoutError)


def test_bad_request_is_permanent(make_provider: Callable[[str], AIProvider]) -> None:
    with pytest.raises(ProviderPermanentError):
        make_provider("bad_request").extract(_request())


def test_timeout_is_transient_without_usage(make_provider: Callable[[str], AIProvider]) -> None:
    with pytest.raises(ProviderTimeoutError) as info:
        make_provider("timeout").extract(_request())
    assert isinstance(info.value, ProviderTransientError)
    assert info.value.usage is None


def test_every_scenario_is_covered() -> None:
    assert set(_ANTHROPIC_REPLIES) == set(SCENARIOS)
    for scenario in SCENARIOS:
        _fake(scenario)


def test_tool_schema_matches_tool_output_model() -> None:
    item_schema = TOOL_INPUT_SCHEMA["properties"]["items"]["items"]
    assert set(item_schema["properties"]) == set(ExtractedItem.model_fields)
    required = {n for n, f in ExtractedItem.model_fields.items() if f.is_required()}
    assert set(item_schema["required"]) == required
    assert set(TOOL_INPUT_SCHEMA["properties"]) == set(ToolOutput.model_fields)
    assert "$ref" not in str(TOOL_INPUT_SCHEMA)

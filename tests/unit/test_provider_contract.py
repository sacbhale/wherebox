"""Shared contract tests: every provider behaves the same for the same scenario (AC-26).

Each scenario is set up per provider: FakeProvider is scripted with the equivalent step, the
Anthropic and OpenRouter adapters answer from recorded responses over httpx MockTransport (no
network). Token counts must match; OpenRouter also reports ``cost_usd``.
"""

import json
from collections.abc import Callable
from typing import Any

import httpx2
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
    strict_json_schema,
)
from tests.unit import openrouter_mock
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


_OPENROUTER_REPLIES = {
    "success": openrouter_mock.reply_json(200, "tools_success"),
    "invalid_output": openrouter_mock.reply_json(200, "invalid_output"),
    "rate_limited": openrouter_mock.reply_json(429, "error_429"),
    "server_error": openrouter_mock.reply_json(500, "error_500"),
    "bad_request": openrouter_mock.reply_json(400, "error_400"),
    "timeout": openrouter_mock.reply_timeout,
}


def _openrouter(scenario: str) -> AIProvider:
    reply = _OPENROUTER_REPLIES[scenario]
    return openrouter_mock.build_mock_openrouter(reply, openrouter_mock.model_info()).provider


@pytest.fixture(params=["fake", "anthropic", "openrouter"])
def make_provider(request: pytest.FixtureRequest) -> Callable[[str], AIProvider]:
    if request.param == "fake":
        return _fake
    if request.param == "openrouter":
        return _openrouter
    return lambda scenario: build_mock_anthropic(_ANTHROPIC_REPLIES[scenario]).provider


def _tokens(usage: Usage | None) -> tuple[int, int] | None:
    return None if usage is None else (usage.input_tokens, usage.output_tokens)


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
    assert _tokens(result.usage) == _tokens(SUCCESS_USAGE)


def test_invalid_output_carries_usage_and_raw(make_provider: Callable[[str], AIProvider]) -> None:
    with pytest.raises(ProviderInvalidOutput) as info:
        make_provider("invalid_output").extract(_request())
    assert _tokens(info.value.usage) == _tokens(INVALID_USAGE)
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
    assert set(_OPENROUTER_REPLIES) == set(SCENARIOS)
    for scenario in SCENARIOS:
        _fake(scenario)


def test_tool_schema_matches_tool_output_model() -> None:
    item_schema = TOOL_INPUT_SCHEMA["properties"]["items"]["items"]
    assert set(item_schema["properties"]) == set(ExtractedItem.model_fields)
    required = {n for n, f in ExtractedItem.model_fields.items() if f.is_required()}
    assert set(item_schema["required"]) == required
    assert set(TOOL_INPUT_SCHEMA["properties"]) == set(ToolOutput.model_fields)
    assert "$ref" not in str(TOOL_INPUT_SCHEMA)


def test_strict_json_schema() -> None:
    strict = strict_json_schema()
    item = strict["properties"]["items"]["items"]
    receipt = strict["properties"]["receipt"]
    for node in (strict, item, receipt):
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    # Optional properties were already nullable, so requiring them loses nothing.
    optional = set(item["properties"]) - set(
        TOOL_INPUT_SCHEMA["properties"]["items"]["items"]["required"]
    )
    for name in optional:
        assert "null" in item["properties"][name]["type"], name
    assert "null" in receipt["type"]
    text = str(strict)
    for bound in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
        assert bound not in text
    assert "$ref" not in text
    assert item["properties"]["kind"]["enum"] == ["consumable", "belonging"]
    # The tool schema itself is untouched, and each call returns a fresh copy.
    assert TOOL_INPUT_SCHEMA["properties"]["items"]["items"]["properties"]["quantity"] == {
        "type": "integer",
        "minimum": 1,
    }
    assert "additionalProperties" not in TOOL_INPUT_SCHEMA
    strict["properties"].clear()
    assert strict_json_schema()["properties"]


def _with_overview(name: str, overview: object) -> AIProvider:
    """A provider whose success answer carries ``overview_image`` = ``overview``."""
    if name == "fake":
        tool_input = SUCCESS["content"][0]["input"] | {"overview_image": overview}
        return FakeProvider([ExtractResult.model_validate(tool_input | {"usage": {}})])
    if name == "anthropic":
        body = recorded("success")
        body["content"][0]["input"]["overview_image"] = overview
        return build_mock_anthropic(lambda _request: httpx2.Response(200, json=body)).provider
    body = openrouter_mock.recorded("tools_success")
    call = body["choices"][0]["message"]["tool_calls"][0]["function"]
    call["arguments"] = json.dumps(json.loads(call["arguments"]) | {"overview_image": overview})
    reply = openrouter_mock.reply_body(200, body)
    return openrouter_mock.build_mock_openrouter(reply, openrouter_mock.model_info()).provider


@pytest.mark.parametrize("name", ["fake", "anthropic", "openrouter"])
@pytest.mark.parametrize(("overview", "kept"), [(1, 1), (0, 0), ("1", None), (-2, None)])
def test_overview_image_passes_through_and_never_fails(
    name: str, overview: object, kept: int | None
) -> None:
    result = _with_overview(name, overview).extract(_request())
    assert result.overview_image == kept
    assert [i.name for i in result.items] == ["Pasta", "Duct tape"]

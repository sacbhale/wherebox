"""OpenRouter adapter: request shape per output strategy, usage/cost, error mapping, the
strategy fallback on "No endpoints found that support the provided '…'" and Retry-After."""

import base64
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.ai import openrouter_adapter
from app.ai.openrouter_adapter import (
    DEFAULT_MAX_TOKENS,
    DOWNGRADE_TTL_S,
    INSUFFICIENT_CREDITS,
    MAX_JSON_TEXT_CHARS,
    MAX_RESPONSE_BYTES,
    TRUNCATED_BY_REASONING,
    OpenRouterProvider,
    first_json_object,
    model_gone_message,
    parse_retry_after,
)
from app.ai.provider import (
    MAX_ERROR_CHARS,
    ProviderError,
    ProviderInvalidOutput,
    ProviderPermanentError,
    ProviderRateLimited,
    ProviderTimeoutError,
    ProviderTransientError,
    capped_retry_after,
    ledger_outcome,
)
from app.ai.schemas import (
    BOX_OUTPUT_TOKENS,
    TOOL_INPUT_SCHEMA,
    TOOL_NAME,
    ExtractRequest,
    Usage,
    strict_json_schema,
)
from app.capture.budget import cost_micros_for
from app.config import Settings
from tests.unit.openrouter_mock import (
    API_KEY,
    MODEL,
    build_mock_openrouter,
    model_info,
    recorded,
    reply_body,
    reply_connect_error,
    reply_json,
    reply_timeout,
)

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
SHELF = ExtractRequest(mode="shelf", images=[JPEG, JPEG], timeout_s=12)
NO_TOOL_USAGE = Usage(input_tokens=800, output_tokens=12, cost_usd=0.000218)
EXPECTED = [("Pasta", 3, "consumable"), ("Duct tape", 1, "belonging")]


def _names(result) -> list[tuple[str, int, str]]:
    return [(i.name, i.quantity, i.kind) for i in result.items]


# ---------------------------------------------------------------- request shape


def test_tools_strategy_request_shape() -> None:
    mock = build_mock_openrouter(reply_json(200, "tools_success"), model_info("tools"))
    result = mock.provider.extract(SHELF)
    assert _names(result) == EXPECTED
    assert len(mock.requests) == 1  # no retries
    request = mock.requests[0]
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert request.headers["x-title"] == "Household Inventory"
    assert "http-referer" not in request.headers  # privacy: no household hostname
    assert "referer" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == MODEL
    assert body["max_tokens"] == BOX_OUTPUT_TOKENS  # photo modes: boxes need a larger cap
    assert body["provider"] == {"require_parameters": True}
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": TOOL_NAME,
                "description": body["tools"][0]["function"]["description"],
                "parameters": TOOL_INPUT_SCHEMA,
            },
        }
    ]
    assert body["tool_choice"] == {"type": "function", "function": {"name": TOOL_NAME}}
    assert "response_format" not in body
    system, user = body["messages"]
    assert system["role"] == "system" and TOOL_NAME in system["content"]
    assert "No tools are available" not in system["content"]
    parts = user["content"]
    assert parts[0]["type"] == "text"
    images = [part for part in parts if part["type"] == "image_url"]
    assert len(images) == 2
    prefix = "data:image/jpeg;base64,"
    assert images[0]["image_url"]["url"].startswith(prefix)
    assert base64.b64decode(images[0]["image_url"]["url"][len(prefix) :]) == JPEG


def test_parallel_tool_calls_off_only_when_the_model_lists_it() -> None:
    # require_parameters would leave no provider for a model that does not list it.
    mock = build_mock_openrouter(reply_json(200, "tools_success"), model_info("tools"))
    mock.provider.extract(SHELF)
    assert "parallel_tool_calls" not in mock.bodies()[0]
    info = model_info("tools", parallel_tool_calls=True)
    mock = build_mock_openrouter(reply_json(200, "tools_success"), info)
    mock.provider.extract(SHELF)
    assert mock.bodies()[0]["parallel_tool_calls"] is False
    info = model_info("json_schema", parallel_tool_calls=True)
    mock = build_mock_openrouter(reply_json(200, "json_success"), info)
    mock.provider.extract(SHELF)
    assert "parallel_tool_calls" not in mock.bodies()[0]


def test_low_reasoning_effort_only_when_it_lowers_the_default() -> None:
    mock = build_mock_openrouter(reply_json(200, "tools_success"), model_info())
    mock.provider.extract(SHELF)
    assert "reasoning" not in mock.bodies()[0]
    info = model_info(lower_reasoning=True)
    mock = build_mock_openrouter(reply_json(200, "tools_success"), info)
    mock.provider.extract(SHELF)
    assert mock.bodies()[0]["reasoning"] == {"effort": "low"}


def _tool_call(arguments: object) -> dict:
    return {"type": "function", "function": {"name": TOOL_NAME, "arguments": arguments}}


def _item(name: str) -> dict:
    return {"name": name, "quantity": 1, "kind": "belonging", "confidence": 0.9}


def test_items_of_every_record_items_call_are_merged() -> None:
    body = recorded("tools_success")
    receipt = {"merchant": "Shop", "purchase_date": None, "total_cents": 500}
    body["choices"][0]["message"]["tool_calls"] = [
        _tool_call(json.dumps({"items": [_item("Hammer")], "receipt": None})),
        _tool_call(json.dumps({"items": [_item("Nails")], "receipt": receipt})),
        _tool_call(json.dumps({"items": [], "receipt": receipt | {"merchant": "Other"}})),
    ]
    mock = build_mock_openrouter(reply_body(200, body), model_info())
    result = mock.provider.extract(SHELF)
    assert [i.name for i in result.items] == ["Hammer", "Nails"]
    assert result.receipt is not None and result.receipt.merchant == "Shop"


def test_tool_arguments_delivered_as_an_object() -> None:
    body = recorded("tools_success")
    body["choices"][0]["message"]["tool_calls"] = [_tool_call({"items": [_item("Saw")]})]
    mock = build_mock_openrouter(reply_body(200, body), model_info())
    assert [i.name for i in mock.provider.extract(SHELF).items] == ["Saw"]


def test_unknown_capabilities_use_tools_strategy() -> None:
    mock = build_mock_openrouter(reply_json(200, "tools_success"), info=None)
    assert _names(mock.provider.extract(SHELF)) == EXPECTED
    assert "tools" in mock.bodies()[0]


def test_json_schema_strategy_request_shape() -> None:
    mock = build_mock_openrouter(reply_json(200, "json_success"), model_info("json_schema"))
    result = mock.provider.extract(SHELF)
    assert _names(result) == EXPECTED
    body = mock.bodies()[0]
    assert "tools" not in body and "tool_choice" not in body
    assert body["provider"] == {"require_parameters": True}
    assert body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": TOOL_NAME, "strict": True, "schema": strict_json_schema()},
    }
    system = body["messages"][0]["content"]
    assert "No tools are available" in system
    assert json.dumps(TOOL_INPUT_SCHEMA) not in system  # response_format carries the schema


def test_prompt_strategy_parses_fenced_json() -> None:
    mock = build_mock_openrouter(reply_json(200, "prompt_success"), model_info("prompt"))
    result = mock.provider.extract(SHELF)
    assert _names(result) == EXPECTED
    body = mock.bodies()[0]
    for key in ("tools", "tool_choice", "response_format", "provider"):
        assert key not in body
    assert json.dumps(TOOL_INPUT_SCHEMA) in body["messages"][0]["content"]
    assert body["max_tokens"] == BOX_OUTPUT_TOKENS


def test_text_mode_sends_no_images() -> None:
    mock = build_mock_openrouter(reply_json(200, "tools_success"), model_info())
    mock.provider.extract(
        ExtractRequest(mode="text", text="two milks in the garage", known_locations=["Garage"])
    )
    parts = mock.bodies()[0]["messages"][1]["content"]
    assert [part["type"] for part in parts] == ["text"]
    assert "Garage" in parts[0]["text"]


def test_image_mode_on_text_only_model_fails_before_calling() -> None:
    mock = build_mock_openrouter(reply_json(200, "tools_success"), model_info(accepts_images=False))
    with pytest.raises(ProviderPermanentError, match=f"Model {MODEL} does not accept images"):
        mock.provider.extract(SHELF)
    assert mock.requests == []
    # Text captures still work on that model.
    mock.provider.extract(ExtractRequest(mode="text", text="milk"))
    assert len(mock.requests) == 1


# ---------------------------------------------------------------- usage and cost


def test_usage_includes_reported_cost(make_settings) -> None:
    mock = build_mock_openrouter(reply_json(200, "tools_success"), model_info())
    usage = mock.provider.extract(SHELF).usage
    assert usage == Usage(input_tokens=1234, output_tokens=56, cost_usd=0.0003925)
    # The ledger uses the reported cost, not token prices.
    settings: Settings = make_settings(ai_input_price_per_mtok=3, ai_output_price_per_mtok=15)
    assert cost_micros_for(usage, settings) == round(0.0003925 * 1_000_000)


def test_missing_cost_is_none() -> None:
    body = recorded("tools_success")
    del body["usage"]["cost"]
    mock = build_mock_openrouter(reply_body(200, body), model_info())
    assert mock.provider.extract(SHELF).usage == Usage(input_tokens=1234, output_tokens=56)


# ---------------------------------------------------------------- invalid output


@pytest.mark.parametrize(
    ("name", "strategy", "usage", "message"),
    [
        ("invalid_output", "tools", Usage(input_tokens=900, output_tokens=40, cost_usd=0.000285),
         "validation"),
        ("no_tool_call", "tools", NO_TOOL_USAGE, "no record_items tool call"),
        ("no_tool_call", "prompt", NO_TOOL_USAGE, "no JSON object"),
        ("truncated", "json_schema", Usage(input_tokens=700, output_tokens=4096, cost_usd=0.006319),
         "output truncated"),
    ],
)  # fmt: skip
def test_bad_output_is_invalid_with_usage_and_raw(
    name: str, strategy, usage: Usage, message: str
) -> None:
    mock = build_mock_openrouter(reply_json(200, name), model_info(strategy))
    with pytest.raises(ProviderInvalidOutput, match=message) as info:
        mock.provider.extract(SHELF)
    assert info.value.usage == usage
    assert info.value.raw
    assert ledger_outcome(info.value) == "invalid_output"


def test_tool_arguments_that_are_not_json() -> None:
    body = recorded("tools_success")
    body["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] = "{not json"
    mock = build_mock_openrouter(reply_body(200, body), model_info())
    with pytest.raises(ProviderInvalidOutput, match="arguments are not JSON") as info:
        mock.provider.extract(SHELF)
    assert info.value.raw == "{not json"


def test_empty_truncated_answer_keeps_raw_message() -> None:
    body = recorded("truncated")
    body["choices"][0]["message"]["content"] = ""
    mock = build_mock_openrouter(reply_body(200, body), model_info("json_schema"))
    with pytest.raises(ProviderInvalidOutput, match="output truncated") as info:
        mock.provider.extract(SHELF)
    assert info.value.raw and info.value.usage.output_tokens == 4096


def test_truncated_by_reasoning() -> None:
    body = recorded("truncated")
    message = body["choices"][0]["message"]
    message["content"] = None
    message["reasoning"] = "Let me look at the shelf carefully..."
    mock = build_mock_openrouter(reply_body(200, body), model_info("json_schema"))
    with pytest.raises(ProviderInvalidOutput) as info:
        mock.provider.extract(SHELF)
    assert str(info.value) == TRUNCATED_BY_REASONING
    # With some visible output the plain message stays.
    message["content"] = '{"items": ['
    mock = build_mock_openrouter(reply_body(200, body), model_info("json_schema"))
    with pytest.raises(ProviderInvalidOutput) as info:
        mock.provider.extract(SHELF)
    assert str(info.value) == "output truncated"


def test_empty_choices_is_invalid_output() -> None:
    body = recorded("tools_success") | {"choices": []}
    mock = build_mock_openrouter(reply_body(200, body), model_info())
    with pytest.raises(ProviderInvalidOutput) as info:
        mock.provider.extract(SHELF)
    assert info.value.usage.input_tokens == 1234


def test_first_json_object_only_searches_a_bounded_prefix() -> None:
    far = " " * MAX_JSON_TEXT_CHARS + '{"a": 1}'
    with pytest.raises(ValueError):
        first_json_object(far)
    assert first_json_object(" " * (MAX_JSON_TEXT_CHARS - 10) + '{"a": 1}') == {"a": 1}


def test_first_json_object() -> None:
    assert first_json_object('noise {"a": 1} {"b": 2}') == {"a": 1}
    assert first_json_object('```json\n{"items": []}\n```') == {"items": []}
    assert first_json_object('{broken {"ok": true}') == {"ok": True}
    with pytest.raises(ValueError):
        first_json_object("[1, 2] no object")


# ---------------------------------------------------------------- errors


@pytest.mark.parametrize(
    ("status", "name", "kind", "message"),
    [
        (400, "error_400", ProviderPermanentError, "OpenRouter HTTP 400: Provider returned error"),
        (401, "error_401", ProviderPermanentError, "OpenRouter HTTP 401: User not found."),
        (402, "error_402", ProviderPermanentError, INSUFFICIENT_CREDITS),
        (403, "error_403", ProviderPermanentError, "OpenRouter HTTP 403"),
        (404, "error_404", ProviderPermanentError, model_gone_message(MODEL)),
        (400, "error_400_invalid_model", ProviderPermanentError, model_gone_message(MODEL)),
        (408, "error_408", ProviderTransientError, "OpenRouter HTTP 408"),
        (429, "error_429", ProviderRateLimited, "OpenRouter HTTP 429: Rate limit exceeded"),
        (500, "error_500", ProviderTransientError, "OpenRouter HTTP 500"),
        (502, "error_502", ProviderTransientError, "OpenRouter HTTP 502"),
        (503, "error_503", ProviderTransientError, "OpenRouter HTTP 503"),
        (200, "error_in_200", ProviderTransientError, "Provider returned error"),
    ],
)
def test_error_mapping(status: int, name: str, kind: type[ProviderError], message: str) -> None:
    mock = build_mock_openrouter(reply_json(status, name), model_info())
    with pytest.raises(kind) as info:
        mock.provider.extract(SHELF)
    assert type(info.value) is kind
    assert message in str(info.value)
    assert info.value.usage is None
    assert len(mock.requests) == 1


def test_error_in_200_with_client_error_code_is_permanent() -> None:
    body = {"error": {"code": 400, "message": "context length exceeded"}}
    mock = build_mock_openrouter(reply_body(200, body), model_info())
    with pytest.raises(ProviderPermanentError, match="context length exceeded"):
        mock.provider.extract(SHELF)


def test_finish_reason_error_is_transient() -> None:
    body = recorded("json_success")
    body["choices"][0]["finish_reason"] = "error"
    mock = build_mock_openrouter(reply_body(200, body), model_info("json_schema"))
    with pytest.raises(ProviderTransientError) as info:
        mock.provider.extract(SHELF)
    assert info.value.usage == Usage(input_tokens=1234, output_tokens=56, cost_usd=0.0003925)


def test_error_body_with_usage_keeps_it() -> None:
    body = recorded("error_502") | {
        "usage": {"prompt_tokens": 50, "completion_tokens": 0, "cost": 0.00001}
    }
    mock = build_mock_openrouter(reply_body(502, body), model_info())
    with pytest.raises(ProviderTransientError) as info:
        mock.provider.extract(SHELF)
    assert info.value.usage == Usage(input_tokens=50, output_tokens=0, cost_usd=0.00001)


def test_non_json_error_body() -> None:
    mock = build_mock_openrouter(
        lambda request: httpx.Response(503, text="upstream down"), model_info()
    )
    with pytest.raises(ProviderTransientError, match="OpenRouter HTTP 503"):
        mock.provider.extract(SHELF)
    mock = build_mock_openrouter(lambda request: httpx.Response(200, text="<html>"), model_info())
    with pytest.raises(ProviderTransientError, match="not JSON") as info:
        mock.provider.extract(SHELF)
    assert info.value.maybe_billed  # the provider answered, so the call was probably billed


def test_timeout_maps_to_timeout_error() -> None:
    mock = build_mock_openrouter(reply_timeout, model_info())
    with pytest.raises(ProviderTimeoutError) as info:
        mock.provider.extract(SHELF)
    assert info.value.usage is None
    assert ledger_outcome(info.value) == "timeout"


def test_timeouts_after_sending_may_be_billed() -> None:
    # A read timeout: the request reached OpenRouter, which may bill it.
    mock = build_mock_openrouter(reply_timeout, model_info())
    with pytest.raises(ProviderTimeoutError) as info:
        mock.provider.extract(SHELF)
    assert info.value.maybe_billed

    def connect_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("no route", request=request)

    mock = build_mock_openrouter(connect_timeout, model_info())
    with pytest.raises(ProviderTimeoutError) as info:
        mock.provider.extract(SHELF)
    assert not info.value.maybe_billed


def test_dropped_connection_after_sending_may_be_billed() -> None:
    def dropped(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("peer closed", request=request)

    mock = build_mock_openrouter(dropped, model_info())
    with pytest.raises(ProviderTransientError) as info:
        mock.provider.extract(SHELF)
    assert info.value.maybe_billed
    mock = build_mock_openrouter(reply_connect_error, model_info())
    with pytest.raises(ProviderTransientError) as info:
        mock.provider.extract(SHELF)
    assert not info.value.maybe_billed


def test_slow_trickle_hits_the_total_deadline() -> None:
    """httpx timeouts are per read: a body trickling in forever must still time out."""

    def trickle() -> Iterator[bytes]:
        for _ in range(200):
            time.sleep(0.02)
            yield b" "

    mock = build_mock_openrouter(lambda request: httpx.Response(200, content=trickle()))
    request = ExtractRequest(mode="text", text="stuff", timeout_s=0.2)
    started = time.monotonic()
    with pytest.raises(ProviderTimeoutError) as info:
        mock.provider.extract(request)
    assert time.monotonic() - started < 1.5
    assert info.value.maybe_billed


def test_oversized_response_is_rejected() -> None:
    chunk = b" " * 65_536

    def huge() -> Iterator[bytes]:
        for _ in range(MAX_RESPONSE_BYTES // len(chunk) + 2):
            yield chunk

    mock = build_mock_openrouter(lambda request: httpx.Response(200, content=huge()))
    with pytest.raises(ProviderInvalidOutput, match="too large") as info:
        mock.provider.extract(SHELF)
    assert info.value.maybe_billed and info.value.usage == Usage()


def test_connect_timeout_is_capped() -> None:
    seen: list[dict] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=recorded("tools_success"))

    slow = ExtractRequest(mode="shelf", images=[JPEG], timeout_s=120)
    build_mock_openrouter(reply, model_info()).provider.extract(slow)
    assert seen[0]["connect"] == 10 and seen[0]["read"] == 120


def test_providers_share_one_http_client() -> None:
    first = OpenRouterProvider(API_KEY, MODEL)
    second = OpenRouterProvider(API_KEY, "openai/gpt-5-nano")
    assert first._client is second._client


def test_timeout_is_passed_to_httpx() -> None:
    seen: list[dict] = []

    def reply(request: httpx.Request) -> httpx.Response:
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json=recorded("tools_success"))

    build_mock_openrouter(reply, model_info()).provider.extract(SHELF)
    assert seen[0]["read"] == 12


def test_connection_error_maps_to_transient() -> None:
    mock = build_mock_openrouter(reply_connect_error, model_info())
    with pytest.raises(ProviderTransientError) as info:
        mock.provider.extract(SHELF)
    assert not isinstance(info.value, ProviderTimeoutError)


@pytest.mark.parametrize("name", ["error_401", "error_402", "error_429", "error_500"])
def test_key_never_appears_in_errors(name: str) -> None:
    body = recorded(name)
    body["error"]["message"] += f" (key {API_KEY})"  # even if echoed back, it is redacted
    status = body["error"]["code"]
    mock = build_mock_openrouter(reply_body(status, body), model_info())
    with pytest.raises(ProviderError) as info:
        mock.provider.extract(SHELF)
    assert API_KEY not in str(info.value)
    assert API_KEY not in repr(info.value.args)


def test_key_not_in_request_body() -> None:
    mock = build_mock_openrouter(reply_json(200, "tools_success"), model_info())
    mock.provider.extract(SHELF)
    assert API_KEY.encode() not in mock.requests[0].content


# ---------------------------------------------------------------- multi-photo (spec-multiphoto)


def test_each_image_is_preceded_by_its_label_and_caps_follow_the_mode() -> None:
    mock = build_mock_openrouter(reply_json(200, "tools_success"), model_info())
    first, second, third = b"\xff\xd8one", b"\xff\xd8two", b"\xff\xd8three"
    mock.provider.extract(ExtractRequest(mode="extend", images=[first, second, third]))
    body = mock.bodies()[0]
    parts = body["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["text"] + ["text", "image_url"] * 3
    assert [p["text"] for p in parts[1::2]] == ["Image 0:", "Image 1:", "Image 2:"]
    prefix = "data:image/jpeg;base64,"
    sent = [base64.b64decode(p["image_url"]["url"][len(prefix) :]) for p in parts[2::2]]
    assert sent == [first, second, third]  # in order
    assert body["max_tokens"] == BOX_OUTPUT_TOKENS
    assert "There are 3 images (Image 0 to Image 2)." in parts[0]["text"]
    for mode in ("receipt", "text"):
        mock.provider.extract(ExtractRequest(mode=mode, images=[first], text="x"))
        assert mock.bodies()[-1]["max_tokens"] == DEFAULT_MAX_TOKENS


# ---------------------------------------------------------------- strategy fallback


def _no_endpoints(param: str) -> dict:
    return {
        "error": {
            "code": 404,
            "message": f"No endpoints found that support the provided '{param}' value",
        }
    }


def _replies(*replies):
    """A reply that answers each request with the next of ``replies`` (status, body)."""
    queue = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = queue.pop(0)
        return httpx.Response(status, json=body)

    return handler


def test_tools_without_endpoints_falls_back_to_json_schema_in_the_same_call() -> None:
    info = model_info("tools")
    mock = build_mock_openrouter(
        _replies(
            (404, _no_endpoints("tool_choice")),
            (200, recorded("json_success")),
            (200, recorded("json_success")),
        ),
        info,
    )
    result = mock.provider.extract(SHELF)
    assert _names(result) == EXPECTED
    assert result.usage == Usage(input_tokens=1234, output_tokens=56, cost_usd=0.0003925)
    first, second = mock.bodies()
    assert "tool_choice" in first and "tools" in first
    assert "tool_choice" not in second and "tools" not in second
    assert second["response_format"]["type"] == "json_schema"
    assert second["provider"] == {"require_parameters": True}
    # The downgrade is remembered: the next call starts at json_schema (one request).
    mock.provider.extract(SHELF)
    assert len(mock.requests) == 3
    assert "tool_choice" not in mock.bodies()[2]
    assert "response_format" in mock.bodies()[2]
    # Another provider object for the same model (a later capture) starts there too.
    other = build_mock_openrouter(reply_json(200, "json_success"), info)
    other.provider.extract(SHELF)
    assert "response_format" in other.bodies()[0] and "tools" not in other.bodies()[0]


def test_json_schema_without_endpoints_falls_back_to_prompt() -> None:
    mock = build_mock_openrouter(
        _replies(
            (404, _no_endpoints("response_format")),
            (200, recorded("prompt_success")),
        ),
        model_info("json_schema"),
    )
    assert _names(mock.provider.extract(SHELF)) == EXPECTED
    first, second = mock.bodies()
    assert "response_format" in first
    assert "response_format" not in second and "provider" not in second
    assert "tools" not in second


def test_unsupported_optional_parameter_is_dropped_before_changing_strategy() -> None:
    info = model_info("tools", parallel_tool_calls=True)
    mock = build_mock_openrouter(
        _replies(
            (404, _no_endpoints("parallel_tool_calls")),
            (200, recorded("tools_success")),
        ),
        info,
    )
    assert _names(mock.provider.extract(SHELF)) == EXPECTED
    first, second = mock.bodies()
    assert first["parallel_tool_calls"] is False
    assert "parallel_tool_calls" not in second and "tool_choice" in second


def test_every_strategy_rejected_is_a_clear_permanent_error() -> None:
    mock = build_mock_openrouter(
        _replies(
            (404, _no_endpoints("tool_choice")),
            (404, _no_endpoints("structured_outputs")),
            # Under 200 with an error code, as OpenRouter sometimes answers.
            (200, _no_endpoints("max_tokens")),
        ),
        model_info("tools"),
    )
    with pytest.raises(ProviderPermanentError) as info:
        mock.provider.extract(SHELF)
    assert type(info.value) is ProviderPermanentError
    assert str(info.value) == f"No provider for {MODEL} supports structured output right now"
    assert info.value.usage is None and not info.value.maybe_billed
    assert len(mock.requests) == 3


def test_model_not_found_404_is_unchanged_by_the_fallback() -> None:
    mock = build_mock_openrouter(reply_json(404, "error_404"), model_info("tools"))
    with pytest.raises(ProviderPermanentError, match="no longer available"):
        mock.provider.extract(SHELF)
    assert len(mock.requests) == 1


# ---------------------------------------------------------------- Retry-After


def test_parse_retry_after() -> None:
    now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("17", now) == 17
    assert parse_retry_after(" 2.5 ", now) == 2.5
    assert parse_retry_after("Thu, 01 Oct 2026 12:01:30 GMT", now) == 90
    assert parse_retry_after("Thu, 01 Oct 2026 11:59:00 GMT", now) == 0  # already past
    assert parse_retry_after(None, now) is None
    assert parse_retry_after("", now) is None
    assert parse_retry_after("soon", now) is None
    assert parse_retry_after("-5", now) == 0


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        ("1e14", 3600),
        ("1e300", 3600),
        ("86400", 3600),
        ("-5", 0),
        ("-1e300", 0),
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("Fri, 31 Dec 9999 23:59:59 GMT", 3600),
    ],
)
def test_parse_retry_after_is_clamped_at_the_source(value: str, seconds: float | None) -> None:
    now = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after(value, now) == seconds


@pytest.mark.parametrize("asked", [1e14, 1e300, float("inf"), float("nan"), -5.0, None])
def test_capped_retry_after_never_overflows(asked: float | None) -> None:
    cap = timedelta(minutes=10)
    delay = capped_retry_after(ProviderRateLimited("busy", retry_after=asked), cap)
    expected = cap if asked is not None and asked > 0 and asked == asked else timedelta()
    assert delay == expected
    datetime(2026, 10, 1, tzinfo=UTC) + delay  # usable as a not_before offset


@pytest.mark.parametrize(
    ("status", "name", "kind"),
    [(429, "error_429", ProviderRateLimited), (503, "error_503", ProviderTransientError)],
)
def test_retry_after_is_carried_on_transient_errors(status, name, kind) -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=recorded(name), headers={"Retry-After": "42"})

    mock = build_mock_openrouter(reply, model_info())
    with pytest.raises(kind) as info:
        mock.provider.extract(SHELF)
    assert info.value.retry_after == 42
    assert ledger_outcome(info.value) == "transient_error"
    without = build_mock_openrouter(reply_json(status, name), model_info())
    with pytest.raises(kind) as info:
        without.provider.extract(SHELF)
    assert info.value.retry_after is None


def test_header_overflow_reaches_the_error_clamped() -> None:
    def reply(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=recorded("error_429"), headers={"Retry-After": "1e300"})

    with pytest.raises(ProviderRateLimited) as info:
        build_mock_openrouter(reply, model_info()).provider.extract(SHELF)
    assert info.value.retry_after == 3600


# ---------------------------------------------------------------- bounded provider text


def test_provider_error_text_is_bounded_and_redacted() -> None:
    leaked = "sk-or-v1-0123456789abcdefABCDEF"
    noise = "line\n\t  " * 2000
    body = {"error": {"code": 400, "message": f"Bad {leaked} input\n{noise}"}}
    mock = build_mock_openrouter(reply_body(400, body), model_info())
    with pytest.raises(ProviderPermanentError) as info:
        mock.provider.extract(SHELF)
    message = str(info.value)
    assert message.startswith("OpenRouter HTTP 400: Bad *** input line line")
    assert len(message) <= MAX_ERROR_CHARS + len("OpenRouter HTTP 400: ")
    assert "\n" not in message and "\t" not in message and "  " not in message
    assert leaked not in message and "sk-or-" not in message


def test_key_cut_at_the_length_limit_is_still_redacted() -> None:
    # The key starts 9 chars before the cut, so bounding first would keep "sk-or-v1-": too short
    # for the key-shaped pattern and no longer an exact match. Redacting first removes it.
    filler = "x" * (MAX_ERROR_CHARS - 1 - len("sk-or-v1-"))
    body = {"error": {"code": 400, "message": filler + API_KEY}}
    mock = build_mock_openrouter(reply_body(400, body), model_info())
    with pytest.raises(ProviderPermanentError) as info:
        mock.provider.extract(SHELF)
    assert "sk-or-" not in str(info.value)


def test_logged_parameter_name_is_capped(caplog: pytest.LogCaptureFixture) -> None:
    param = "p" * 400
    mock = build_mock_openrouter(
        _replies((404, _no_endpoints(param)), (200, recorded("json_success"))), model_info()
    )
    with caplog.at_level(logging.INFO, logger="app.ai.openrouter_adapter"):
        mock.provider.extract(SHELF)
    (record,) = [r for r in caplog.records if "no endpoint supports" in r.getMessage()]
    assert "p" * 64 in record.getMessage() and "p" * 65 not in record.getMessage()


# ---------------------------------------------------------------- 404 variants


def test_generic_no_endpoints_404_falls_back_to_the_next_strategy() -> None:
    mock = build_mock_openrouter(
        _replies(
            (404, recorded("error_404_no_endpoints")),
            (200, recorded("json_success")),
        ),
        model_info("tools"),
    )
    assert _names(mock.provider.extract(SHELF)) == EXPECTED
    first, second = mock.bodies()
    assert "tool_choice" in first
    assert "tools" not in second and second["response_format"]["type"] == "json_schema"


def test_no_endpoints_for_the_model_stays_permanent_with_openrouters_message() -> None:
    message = f"No endpoints found for {MODEL}."
    mock = build_mock_openrouter(
        reply_body(404, {"error": {"code": 404, "message": message}}), model_info("tools")
    )
    with pytest.raises(ProviderPermanentError) as info:
        mock.provider.extract(SHELF)
    assert str(info.value) == f"OpenRouter HTTP 404: {message}"
    assert len(mock.requests) == 1


def test_exhausted_strategies_keep_the_last_404_as_detail() -> None:
    generic = recorded("error_404_no_endpoints")
    mock = build_mock_openrouter(
        _replies((404, _no_endpoints("tool_choice")), (404, generic), (404, generic)),
        model_info("tools"),
    )
    with pytest.raises(ProviderPermanentError) as info:
        mock.provider.extract(SHELF)
    assert str(info.value) == f"No provider for {MODEL} supports structured output right now"
    assert info.value.detail == (
        "OpenRouter HTTP 404: No endpoints found that can handle the requested parameters."
    )
    assert info.value.__cause__ is not None


def test_model_gone_keeps_openrouters_message_as_detail() -> None:
    mock = build_mock_openrouter(reply_json(404, "error_404"), model_info())
    with pytest.raises(ProviderPermanentError) as info:
        mock.provider.extract(SHELF)
    assert str(info.value) == model_gone_message(MODEL)
    assert info.value.detail == (
        "OpenRouter HTTP 404: No allowed providers are available for the selected model."
    )


# ---------------------------------------------------------------- downgrade memo


def test_downgrade_is_remembered_once_the_fallback_is_accepted_even_if_the_call_fails() -> None:
    info = model_info("tools")
    mock = build_mock_openrouter(
        _replies((404, _no_endpoints("tool_choice")), (429, recorded("error_429"))), info
    )
    with pytest.raises(ProviderRateLimited):
        mock.provider.extract(SHELF)
    later = build_mock_openrouter(reply_json(200, "json_success"), info)
    later.provider.extract(SHELF)
    assert "tools" not in later.bodies()[0] and "response_format" in later.bodies()[0]


def test_downgrades_merge_monotonically() -> None:
    openrouter_adapter._remember(MODEL, "prompt", frozenset({"reasoning"}))
    # A concurrent call that only saw the first fallback cannot move the model back up.
    openrouter_adapter._remember(MODEL, "json_schema", frozenset({"parallel_tool_calls"}))
    assert openrouter_adapter._remembered(MODEL) == (
        "prompt",
        frozenset({"reasoning", "parallel_tool_calls"}),
    )
    mock = build_mock_openrouter(reply_json(200, "prompt_success"), model_info("tools"))
    mock.provider.extract(SHELF)
    body = mock.bodies()[0]
    assert "tools" not in body and "response_format" not in body and "provider" not in body


def test_a_downgrade_expires_after_the_ttl() -> None:
    info = model_info("tools")
    openrouter_adapter._remember(MODEL, "json_schema", frozenset())
    strategy, dropped, saved = openrouter_adapter._downgrades[MODEL]
    openrouter_adapter._downgrades[MODEL] = (strategy, dropped, saved - DOWNGRADE_TTL_S - 1)
    mock = build_mock_openrouter(reply_json(200, "tools_success"), info)
    mock.provider.extract(SHELF)
    assert "tool_choice" in mock.bodies()[0]  # back at the catalog strategy
    assert MODEL not in openrouter_adapter._downgrades
    # An expired entry does not count in a merge either.
    openrouter_adapter._remember(MODEL, "prompt", frozenset())
    strategy, dropped, saved = openrouter_adapter._downgrades[MODEL]
    openrouter_adapter._downgrades[MODEL] = (strategy, dropped, saved - DOWNGRADE_TTL_S - 1)
    openrouter_adapter._remember(MODEL, "json_schema", frozenset())
    assert openrouter_adapter._remembered(MODEL) == ("json_schema", frozenset())


def test_downgrade_memo_is_cleared_between_tests() -> None:
    # tests/conftest.py clears it around every test (the previous tests left entries).
    assert openrouter_adapter._remembered(MODEL) is None

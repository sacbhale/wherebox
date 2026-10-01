"""Anthropic adapter: request shape and the SDK-error → contract-error mapping."""

import base64
import json

import httpx2
import pytest

from app.ai.provider import (
    ProviderInvalidOutput,
    ProviderPermanentError,
    ProviderRateLimited,
    ProviderTimeoutError,
    ProviderTransientError,
    ledger_outcome,
)
from app.ai.schemas import TOOL_INPUT_SCHEMA, TOOL_NAME, ExistingItem, ExtractRequest, Usage
from tests.unit.anthropic_mock import (
    build_mock_anthropic,
    recorded,
    reply_connect_error,
    reply_json,
    reply_timeout,
)

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"


def test_request_uses_forced_tool_images_and_timeout() -> None:
    mock = build_mock_anthropic(reply_json(200, "success"))
    mock.provider.extract(ExtractRequest(mode="shelf", images=[JPEG, JPEG], timeout_s=12))
    assert len(mock.requests) == 1  # no SDK retries
    request = mock.requests[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "sk-test"
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["tool_choice"] == {"type": "tool", "name": TOOL_NAME}
    assert body["tools"] == [
        {
            "name": TOOL_NAME,
            "description": body["tools"][0]["description"],
            "input_schema": TOOL_INPUT_SCHEMA,
        }
    ]
    content = body["messages"][0]["content"]
    images = [block for block in content if block["type"] == "image"]
    assert len(images) == 2
    assert images[0]["source"]["type"] == "base64"
    assert images[0]["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(images[0]["source"]["data"]) == JPEG
    assert content[-1]["type"] == "text"
    assert "record_items" in body["system"]


def test_receipt_prompt_asks_for_product_lines_only() -> None:
    mock = build_mock_anthropic(reply_json(200, "receipt_success"))
    result = mock.provider.extract(ExtractRequest(mode="receipt", images=[JPEG]))
    system = mock.bodies()[0]["system"].lower()
    for word in ("product lines only", "tax", "total", "subtotal", "bag", "deposit", "change"):
        assert word in system
    assert result.receipt is not None and result.receipt.merchant == "Corner Grocery"
    assert [item.price_cents for item in result.items] == [258, 699]


def test_text_prompt_lists_known_locations() -> None:
    mock = build_mock_anthropic(reply_json(200, "success"))
    mock.provider.extract(
        ExtractRequest(
            mode="text", text="two milks and the drill is in the garage", known_locations=["Garage"]
        )
    )
    body = mock.bodies()[0]
    assert all(block["type"] == "text" for block in body["messages"][0]["content"])
    text = body["messages"][0]["content"][0]["text"]
    assert "Garage" in text and "two milks" in text
    assert "location_hint" in body["system"]


def test_reconcile_prompt_lists_existing_items() -> None:
    mock = build_mock_anthropic(reply_json(200, "success"))
    mock.provider.extract(
        ExtractRequest(
            mode="reconcile",
            images=[JPEG],
            existing_items=[ExistingItem(id=41, name="Pasta", quantity=2)],
        )
    )
    body = mock.bodies()[0]
    assert "id=41: Pasta (quantity 2)" in body["messages"][0]["content"][-1]["text"]
    assert "matched_existing_id" in body["system"]


@pytest.mark.parametrize("status", [429, 500, 529])
def test_retryable_statuses_map_to_transient(status: int) -> None:
    mock = build_mock_anthropic(reply_json(status, f"error_{status}"))
    with pytest.raises(ProviderTransientError) as info:
        mock.provider.extract(ExtractRequest(mode="shelf", images=[JPEG]))
    assert str(status) in str(info.value)
    assert ledger_outcome(info.value) == "transient_error"
    assert len(mock.requests) == 1


@pytest.mark.parametrize(
    ("status", "kind"), [(429, ProviderRateLimited), (529, ProviderTransientError)]
)
def test_retry_after_header_is_carried(status: int, kind: type) -> None:
    def reply(request: httpx2.Request) -> httpx2.Response:
        body = recorded(f"error_{status}")
        return httpx2.Response(status, json=body, headers={"retry-after": "12"})

    with pytest.raises(kind) as info:
        build_mock_anthropic(reply).provider.extract(ExtractRequest(mode="shelf", images=[JPEG]))
    assert type(info.value) is kind and info.value.retry_after == 12
    without = build_mock_anthropic(reply_json(status, f"error_{status}"))
    with pytest.raises(kind) as info:
        without.provider.extract(ExtractRequest(mode="shelf", images=[JPEG]))
    assert info.value.retry_after is None


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_client_errors_map_to_permanent(status: int) -> None:
    mock = build_mock_anthropic(reply_json(status, f"error_{status}"))
    with pytest.raises(ProviderPermanentError) as info:
        mock.provider.extract(ExtractRequest(mode="shelf", images=[JPEG]))
    assert info.value.usage is None
    assert ledger_outcome(info.value) == "permanent_error"


def test_status_without_recorded_body_503() -> None:
    mock = build_mock_anthropic(lambda request: httpx2.Response(503, text="upstream down"))
    with pytest.raises(ProviderTransientError):
        mock.provider.extract(ExtractRequest(mode="shelf", images=[JPEG]))


def test_timeout_maps_to_timeout_error() -> None:
    mock = build_mock_anthropic(reply_timeout)
    with pytest.raises(ProviderTimeoutError) as info:
        mock.provider.extract(ExtractRequest(mode="shelf", images=[JPEG]))
    assert info.value.usage is None
    assert ledger_outcome(info.value) == "timeout"


def test_connection_error_maps_to_transient() -> None:
    mock = build_mock_anthropic(reply_connect_error)
    with pytest.raises(ProviderTransientError) as info:
        mock.provider.extract(ExtractRequest(mode="shelf", images=[JPEG]))
    assert not isinstance(info.value, ProviderTimeoutError)


@pytest.mark.parametrize(
    ("name", "usage"),
    [
        ("invalid_tool_output", Usage(input_tokens=900, output_tokens=40)),
        ("no_tool_call", Usage(input_tokens=800, output_tokens=12)),
        ("truncated", Usage(input_tokens=700, output_tokens=4096)),
    ],
)
def test_bad_output_is_invalid_with_usage_and_raw(name: str, usage: Usage) -> None:
    mock = build_mock_anthropic(reply_json(200, name))
    with pytest.raises(ProviderInvalidOutput) as info:
        mock.provider.extract(ExtractRequest(mode="shelf", images=[JPEG]))
    assert info.value.usage == usage
    assert info.value.raw
    assert ledger_outcome(info.value) == "invalid_output"


def test_success_usage_and_ok_outcome() -> None:
    mock = build_mock_anthropic(reply_json(200, "success"))
    result = mock.provider.extract(ExtractRequest(mode="shelf", images=[JPEG]))
    assert result.usage == Usage(input_tokens=1234, output_tokens=56)
    assert ledger_outcome(None) == "ok"

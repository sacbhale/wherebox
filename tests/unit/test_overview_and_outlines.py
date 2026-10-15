"""Location photo and outlines (spec-location-photo R2, R4, R5): ``overview_image`` never fails
a call (schema, validators, both adapters, the fake script, prompts) and its apply-time
fallbacks; user outline validation."""

import json
import math

import httpx2
import pytest

from app.ai import prompts
from app.ai.fake import parse_step
from app.ai.schemas import (
    TOOL_INPUT_SCHEMA,
    ExtractedItem,
    ExtractRequest,
    ExtractResult,
    ToolOutput,
    Usage,
    strict_json_schema,
)
from app.capture.boxes import MIN_USER_BOX, BoxError, overview_photo, parse_user_box
from tests.unit import openrouter_mock
from tests.unit.anthropic_mock import build_mock_anthropic, recorded

PHOTOS = [11, 12, 13]


def _item(name: str, *images: int) -> ExtractedItem:
    boxes = [{"image": i, "box_2d": [100, 100, 300, 300]} for i in images]
    return ExtractedItem(name=name, quantity=1, kind="belonging", confidence=0.9, boxes=boxes)


def _result(overview: object, items: list[ExtractedItem]) -> ExtractResult:
    return ExtractResult(items=items, overview_image=overview, usage=Usage())


# ---------------------------------------------------------------- overview selection


def test_valid_model_index_wins() -> None:
    assert overview_photo(_result(2, [_item("A", 0), _item("B", 0)]), PHOTOS) == 13
    assert overview_photo(_result(0, []), PHOTOS) == 11


@pytest.mark.parametrize("overview", [None, 3, 99, -1, "1", True, False, 1.0, [1], {"i": 1}])
def test_invalid_or_absent_index_falls_back_to_the_most_boxes(overview: object) -> None:
    items = [_item("A", 1), _item("B", 1, 2), _item("C", 0)]
    assert overview_photo(_result(overview, items), PHOTOS) == 12


def test_tie_takes_the_first_photo_and_no_boxes_the_first_photo() -> None:
    assert overview_photo(_result(None, [_item("A", 2), _item("B", 1)]), PHOTOS) == 12
    assert overview_photo(_result(None, [_item("A")]), PHOTOS) == 11
    assert overview_photo(_result(None, []), []) is None


@pytest.mark.parametrize(
    ("raw", "kept"),
    [(0, 0), (4, 4), (-1, None), ("2", None), (True, None), (2.0, None), (None, None), ([], None)],
)
def test_overview_image_never_fails_validation(raw: object, kept: int | None) -> None:
    output = ToolOutput.model_validate({"items": [], "overview_image": raw})
    assert output.overview_image == kept
    assert ExtractResult(items=[], overview_image=raw, usage=Usage()).overview_image == kept
    assert ToolOutput.model_validate({"items": []}).overview_image is None


def test_tool_schema_and_strict_schema_carry_a_nullable_overview_image() -> None:
    assert TOOL_INPUT_SCHEMA["properties"]["overview_image"]["type"] == ["integer", "null"]
    assert "overview_image" not in TOOL_INPUT_SCHEMA["required"]
    strict = strict_json_schema()
    assert "overview_image" in strict["required"]
    assert strict["properties"]["overview_image"]["type"] == ["integer", "null"]


def test_prompts_ask_for_overview_image_in_photo_modes_only() -> None:
    for mode in ("shelf", "reconcile", "extend"):
        assert "Set overview_image to the index N" in prompts.system_prompt(mode)
    for mode in ("receipt", "text"):
        system = prompts.system_prompt(mode)
        assert "Set overview_image to null." in system
        assert "index N" not in system


def test_fake_script_steps_carry_overview_image() -> None:
    step = parse_step({"result": {"items": [], "overview_image": 1}})
    assert step.outcome.overview_image == 1
    bad = parse_step({"result": {"items": [], "overview_image": "wide"}})
    assert bad.outcome.overview_image is None


def test_anthropic_adapter_passes_overview_image_through() -> None:
    body = recorded("success")
    body["content"][0]["input"]["overview_image"] = 1
    mock = build_mock_anthropic(lambda _request: httpx2.Response(200, json=body))
    result = mock.provider.extract(ExtractRequest(mode="shelf", images=[b"a", b"b"]))
    assert result.overview_image == 1
    body["content"][0]["input"]["overview_image"] = "first"
    mock = build_mock_anthropic(lambda _request: httpx2.Response(200, json=body))
    assert mock.provider.extract(ExtractRequest(mode="shelf")).overview_image is None


def test_openrouter_adapter_takes_the_first_non_null_overview_image() -> None:
    body = openrouter_mock.recorded("tools_success")
    item = {"name": "Saw", "quantity": 1, "kind": "belonging", "confidence": 0.9}
    calls = [
        {"items": [item], "overview_image": None},
        {"items": [], "overview_image": 2},
        {"items": [], "overview_image": 0},
    ]
    body["choices"][0]["message"]["tool_calls"] = [
        {"type": "function", "function": {"name": "record_items", "arguments": json.dumps(c)}}
        for c in calls
    ]
    mock = openrouter_mock.build_mock_openrouter(
        openrouter_mock.reply_body(200, body), openrouter_mock.model_info()
    )
    result = mock.provider.extract(ExtractRequest(mode="shelf", images=[b"a", b"b", b"c"]))
    assert result.overview_image == 2 and [i.name for i in result.items] == ["Saw"]


# ---------------------------------------------------------------- user outlines


def _form(x0: object, y0: object, x1: object, y1: object) -> dict[str, str]:
    return {"x0": str(x0), "y0": str(y0), "x1": str(x1), "y1": str(y1)}


def test_user_box_is_parsed() -> None:
    assert parse_user_box(_form(0.1, 0.2, 0.5, 0.6)) == (0.1, 0.2, 0.5, 0.6)
    assert parse_user_box(_form(0, 0, 1, 1)) == (0.0, 0.0, 1.0, 1.0)
    assert parse_user_box(_form(0.5, 0.5, 0.5 + MIN_USER_BOX, 0.6)) == (
        0.5,
        0.5,
        0.5 + MIN_USER_BOX,
        0.6,
    )


@pytest.mark.parametrize(
    "form",
    [
        _form("nan", 0.1, 0.5, 0.5),
        _form(0.1, "inf", 0.5, 0.5),
        _form(0.1, 0.1, "-inf", 0.5),
        _form(0.1, 0.1, 0.5, "1e999"),
        _form("abc", 0.1, 0.5, 0.5),
        _form("", 0.1, 0.5, 0.5),
        {"x0": "0.1", "y0": "0.1", "x1": "0.5"},
        _form(-0.1, 0.1, 0.5, 0.5),
        _form(0.1, 0.1, 1.2, 0.5),
        _form(0.5, 0.1, 0.1, 0.5),
        _form(0.1, 0.5, 0.5, 0.5),
        _form(0.1, 0.1, 0.1 + MIN_USER_BOX / 2, 0.5),
        _form(0.1, 0.1, 0.5, 0.1 + MIN_USER_BOX / 2),
    ],
)
def test_bad_user_boxes_are_rejected(form: dict[str, str]) -> None:
    with pytest.raises(BoxError):
        parse_user_box(form)


def test_rounding_to_six_decimals_cannot_reject_a_minimum_outline() -> None:
    # The page posts 6-decimal strings: a side drawn at the minimum may arrive a hair under it.
    assert parse_user_box(_form(0.1, 0.1, "0.1199995", "0.5")) == (0.1, 0.1, 0.1199995, 0.5)
    with pytest.raises(BoxError):
        parse_user_box(_form(0.1, 0.1, "0.11999", "0.5"))


def test_min_user_box_is_finite_and_small() -> None:
    assert math.isfinite(MIN_USER_BOX) and 0 < MIN_USER_BOX < 0.1


@pytest.mark.parametrize("value", ["nan", "NaN", "inf", "-inf", "1e999"])
def test_non_finite_coordinates_are_refused_as_numbers(value: str) -> None:
    with pytest.raises(BoxError, match="needs four numbers"):
        parse_user_box(_form(value, 0.1, 0.5, 0.5))
    with pytest.raises(BoxError, match="needs four numbers"):
        parse_user_box(_form(0.1, 0.1, 0.5, value))

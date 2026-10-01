"""Boxes from the model (spec-multiphoto Part 3, R3, R16): validation, clamping and conversion
never fail; the tool/strict schemas and prompts describe them; the fake script, both
adapters, the output cap, the timeout and the image-aware estimate follow the image count."""

import json
import math
import sqlite3
from datetime import UTC, datetime

import pytest

from app.ai import prompts
from app.ai.fake import parse_step
from app.ai.schemas import (
    BOX_OUTPUT_TOKENS,
    DEFAULT_OUTPUT_TOKENS,
    TOOL_INPUT_SCHEMA,
    ExistingItem,
    ExtractedItem,
    ExtractRequest,
    ExtractResult,
    Fractions,
    ToolOutput,
    Usage,
    boxes_for_images,
    clean_boxes,
    output_cap,
    strict_json_schema,
    to_fractions,
)
from app.capture import budget, images
from app.capture.boxes import Crop, crop_region
from app.config import Settings
from tests.unit.anthropic_mock import build_mock_anthropic, reply_json

OK = [100, 200, 300, 400]


def entry(image: object = 0, box: object = None) -> dict[str, object]:
    return {"image": image, "box_2d": OK if box is None else box}


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ([entry()], [entry()]),
        ([entry(1, [0, 0, 1000, 1000])], [entry(1, [0, 0, 1000, 1000])]),
        ([entry(0, [-5, -1, 1003, 2000])], [entry(0, [0, 0, 1000, 1000])]),  # clamped
        ([entry(0, [10.4, 20.6, 30.5, 40])], [entry(0, [10, 21, 30, 40])]),  # rounded
        ([entry(0, ["100", 200, 300, 400])], []),  # string coordinate
        ([entry(0, [100, 200, 300])], []),  # 3 numbers
        ([entry(0, [100, 200, 300, 400, 500])], []),
        ([entry(0, "100,200,300,400")], []),
        ([entry(0, [100, 200, math.nan, 400])], []),
        ([entry(0, [100, 200, math.inf, 400])], []),
        ([entry(0, [100, True, 300, 400])], []),  # bool is not a number here
        ([entry(0, [300, 200, 300, 400])], []),  # zero height
        ([entry(0, [100, 400, 300, 200])], []),  # xmin > xmax
        ([entry(0, [-50, 200, -10, 400])], []),  # empty after clamping
        ([entry("0")], []),
        ([entry(-1)], []),
        ([entry(True)], []),
        ([entry(1.0)], []),
        ([{"box_2d": OK}], []),
        ([{"image": 0}], []),
        ([OK], []),
        (["box"], []),
        ([None], []),
        ([entry(0)] * 5, [entry(0)] * 4),  # at most 4 per image
        ([entry(0)] * 5 + [entry(1)], [entry(0)] * 4 + [entry(1)]),
        ([], []),
        ("garbage", None),
        ({"image": 0, "box_2d": OK}, None),
        (None, None),
        (7, None),
    ],
)
def test_clean_boxes_table(raw: object, kept: object) -> None:
    assert clean_boxes(raw) == kept


def test_image_index_is_checked_against_the_images_sent() -> None:
    raw = [entry(0), entry(2), entry(3)]
    assert clean_boxes(raw, 3) == [entry(0), entry(2)]
    assert clean_boxes(raw, 0) == []


def test_extracted_item_never_fails_on_bad_boxes() -> None:
    base = {"name": "Tape", "quantity": 1, "kind": "consumable", "confidence": 0.9}
    for junk in ("x", 5, {"a": 1}, [1, 2], [{"image": "a", "box_2d": "b"}], [[1, 2, 3, 4]]):
        item = ExtractedItem.model_validate(base | {"boxes": junk})
        assert item.boxes in (None, [])
    item = ExtractedItem.model_validate(base | {"boxes": [entry(0), entry("bad")]})
    assert item.boxes == [entry(0)]
    assert ExtractedItem.model_validate(base).boxes is None
    # The whole tool output stays valid (the capture is not failed by boxes).
    output = ToolOutput.model_validate({"items": [base | {"boxes": "nonsense"}]})
    assert output.items[0].name == "Tape"


def test_conversion_to_fractions() -> None:
    assert to_fractions(entry(1, [100, 200, 300, 400])) == Fractions(1, 0.2, 0.1, 0.4, 0.3)
    item = ExtractedItem(
        name="Jar",
        quantity=1,
        kind="consumable",
        confidence=0.9,
        boxes=[entry(0, [0, 750, 250, 1000]), entry(4)],
    )
    assert boxes_for_images(item, 2) == [Fractions(0, 0.75, 0.0, 1.0, 0.25)]
    assert boxes_for_images(item.model_copy(update={"boxes": None}), 2) == []


def test_tool_schema_and_strict_schema_carry_boxes() -> None:
    boxes = TOOL_INPUT_SCHEMA["properties"]["items"]["items"]["properties"]["boxes"]
    assert boxes["type"] == ["array", "null"]
    assert boxes["items"]["required"] == ["image", "box_2d"]
    assert boxes["items"]["properties"]["box_2d"]["minItems"] == 4
    assert "ymin, xmin, ymax, xmax" in boxes["description"]
    strict = strict_json_schema()["properties"]["items"]["items"]["properties"]["boxes"]
    assert strict["items"]["additionalProperties"] is False
    assert strict["items"]["required"] == ["image", "box_2d"]
    for bound in ("minItems", "maxItems", "minimum", "maximum"):
        assert bound not in json.dumps(strict)


@pytest.mark.parametrize("mode", ["shelf", "reconcile", "extend"])
def test_photo_prompts_explain_views_boxes_and_indexes(mode: str) -> None:
    system = prompts.system_prompt(mode)  # type: ignore[arg-type]
    for phrase in (
        "different views",
        "only once",
        '"Image N:"',
        "box_2d",
        "[ymin, xmin, ymax, xmax]",
        "0-based image index",
        "from 0 to 1000",
        "at most 4 boxes",
    ):
        assert phrase in system, phrase
    assert prompts.image_label(3) == "Image 3:"


@pytest.mark.parametrize("mode", ["receipt", "text"])
def test_receipt_and_text_prompts_ask_for_no_boxes(mode: str) -> None:
    system = prompts.system_prompt(mode)  # type: ignore[arg-type]
    assert "Set boxes to null." in system and "box_2d" not in system


def test_extend_prompt_and_existing_items() -> None:
    system = prompts.system_prompt("extend")
    assert "not here any more — do not re-add" in system
    assert "never list it as a new item" in system
    request = ExtractRequest(
        mode="extend",
        images=[b"a", b"b"],
        existing_items=[
            ExistingItem(id=1, name="Tape", quantity=2),
            ExistingItem(id=2, name="Glue", quantity=1, gone=True),
        ],
    )
    text = prompts.user_text(request)
    assert "- id=1: Tape (quantity 2)\n" in text
    assert "- id=2: Glue (quantity 1) — not here any more — do not re-add" in text
    assert "There are 2 images (Image 0 to Image 1)." in text
    single = prompts.user_text(ExtractRequest(mode="shelf", images=[b"a"]))
    assert "There is 1 image (Image 0)." in single


def test_fake_script_steps_carry_boxes() -> None:
    step = parse_step(
        {
            "result": {
                "items": [
                    {
                        "name": "Tape",
                        "quantity": 1,
                        "kind": "consumable",
                        "confidence": 0.9,
                        "boxes": [entry(0), {"image": 0, "box_2d": "junk"}],
                    }
                ]
            }
        }
    )
    assert isinstance(step.outcome, ExtractResult)
    assert step.outcome.items[0].boxes == [entry(0)]


def test_many_items_with_many_boxes_fit_the_output_cap() -> None:
    items = [
        {
            "name": f"Storage box number {i}",
            "quantity": 2,
            "category": "Storage",
            "kind": "belonging",
            "confidence": 0.9,
            "location_hint": None,
            "price_cents": None,
            "purchase_date": None,
            "matched_existing_id": None,
            "boxes": [entry(image, [101, 202, 303, 404]) for image in range(6)],
        }
        for i in range(80)
    ]
    answer = json.dumps({"items": items, "receipt": None})
    # ~4 characters per token: 80 items seen in 6 views each stay under the cap.
    assert len(answer) / 4 < BOX_OUTPUT_TOKENS
    assert len(answer) / 4 > DEFAULT_OUTPUT_TOKENS  # which the old cap would have truncated
    assert len(ToolOutput.model_validate_json(answer).items) == 80


def test_output_cap_by_mode() -> None:
    for mode in ("shelf", "reconcile", "extend"):
        assert output_cap(mode) == BOX_OUTPUT_TOKENS == 16_384
    for mode in ("receipt", "text"):
        assert output_cap(mode) == DEFAULT_OUTPUT_TOKENS == 4096
    assert output_cap("shelf", 32_000) == 32_000


def test_call_timeout_scales_with_images() -> None:
    assert images.call_timeout(60, 0) == 60
    assert images.call_timeout(60, 1) == 60
    assert images.call_timeout(60, 3) == 80
    assert images.call_timeout(45, 12) == 155


def test_anthropic_adapter_labels_images_and_uses_the_mode_cap() -> None:
    mock = build_mock_anthropic(reply_json(200, "success"))
    mock.provider.extract(ExtractRequest(mode="shelf", images=[b"\xff\xd8a", b"\xff\xd8b"]))
    body = mock.bodies()[0]
    content = body["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text", "image", "text", "image", "text"]
    assert [content[0]["text"], content[2]["text"]] == ["Image 0:", "Image 1:"]
    assert body["max_tokens"] == BOX_OUTPUT_TOKENS
    assert "box_2d" in body["system"]
    mock.provider.extract(ExtractRequest(mode="receipt", images=[b"\xff\xd8a"]))
    assert mock.bodies()[1]["max_tokens"] == DEFAULT_OUTPUT_TOKENS


def test_image_aware_token_estimate(conn: sqlite3.Connection, settings: Settings) -> None:
    # No history: 300 + 1,200 per image in, 100 + 300 per image out.
    assert budget.token_estimate(conn, 1) == (1_500, 400, False)
    assert budget.token_estimate(conn, 3) == (3_900, 1_000, False)
    assert budget.token_estimate(conn, 0) == (1_500, 400, False)  # text: per-call default
    now = datetime(2026, 9, 30, tzinfo=UTC)
    usage = Usage(input_tokens=6_300, output_tokens=1_300)
    budget.record_ai_call(conn, settings, None, now, usage, "ok", image_count=3)
    budget.record_ai_call(conn, settings, None, now, Usage(input_tokens=99), "timeout")
    # Per image: (6,300 - 300) / 3 = 2,000 in, (1,300 - 100) / 3 = 400 out.
    assert budget.token_estimate(conn, 1) == (2_300, 500, True)
    assert budget.token_estimate(conn, 6) == (12_300, 2_500, True)
    row = conn.execute("SELECT image_count FROM ai_calls WHERE outcome = 'ok'").fetchone()
    assert row[0] == 3


@pytest.mark.parametrize(
    ("box", "size", "position"),
    [
        ((0.4, 0.4, 0.6, 0.6), "500.00% 500.00%", "50.00% 50.00%"),
        ((0.0, 0.0, 0.5, 1.0), "200.00% 100.00%", "0.00% 0.00%"),
        ((0.0, 0.0, 1.0, 1.0), "100.00% 100.00%", "0.00% 0.00%"),  # whole image: no /0
        ((0.5, 0.0, 1.0, 0.9995), "200.00% 100.00%", "100.00% 0.00%"),
    ],
)
def test_crop_css_values(box: tuple[float, ...], size: str, position: str) -> None:
    crop = Crop(1, 2, 3, 3, *box)
    assert crop.background_size == size
    assert crop.background_position == position
    assert crop.url == "/photos/2"


def test_token_estimate_counts_old_text_rows_as_no_images(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    """Ledger rows from before image counts (NULL): a text capture's sent no image, so it
    stays out of the per-image average; a photo capture's sent one."""
    now = datetime(2026, 9, 30, tzinfo=UTC)

    def capture(kind: str) -> int:
        cursor = conn.execute(
            "INSERT INTO captures (type, status, not_before, created_at)"
            " VALUES (?, 'done', '2026-09-30T00:00:00.000Z', '2026-09-30T00:00:00.000Z')",
            (kind,),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    text_usage = Usage(input_tokens=50_000, output_tokens=9_000)
    budget.record_ai_call(conn, settings, capture("text"), now, text_usage, "ok")
    assert budget.token_estimate(conn, 1) == (1_500, 400, False)  # no image history yet
    photo_usage = Usage(input_tokens=2_300, output_tokens=500)
    budget.record_ai_call(conn, settings, capture("photo"), now, photo_usage, "ok")
    assert budget.token_estimate(conn, 1) == (2_300, 500, True)
    assert budget.token_estimate(conn, 2) == (4_300, 900, True)


def _square_px(region: tuple[float, ...], width: int, height: int) -> tuple[float, float]:
    x0, y0, x1, y1 = region
    return (x1 - x0) * width, (y1 - y0) * height


@pytest.mark.parametrize(
    ("box", "size", "expected"),
    [
        # No display size (older photos): the padded box, clamped (previous behaviour).
        ((0.4, 0.4, 0.6, 0.6), (None, None), (0.38, 0.38, 0.62, 0.62)),
        ((0.0, 0.0, 0.5, 1.0), (None, None), (0.0, 0.0, 0.55, 1.0)),
        # A square photo and box: just the padding.
        ((0.4, 0.4, 0.6, 0.6), (1000, 1000), (0.38, 0.38, 0.62, 0.62)),
        # Portrait 1500x2000: the same fractions are 360x480 px, so it widens to 480 px.
        ((0.4, 0.4, 0.6, 0.6), (1500, 2000), (0.34, 0.38, 0.66, 0.62)),
        # A tall thin box on a landscape photo: 48x1440 px grows to 1440x1440 px.
        ((0.5, 0.1, 0.52, 0.9), (2000, 1500), (0.15, 0.02, 0.87, 0.98)),
        # At the edge: shifted inside, not clipped (it keeps its square size).
        ((0.0, 0.4, 0.1, 0.5), (1000, 1000), (0.0, 0.39, 0.12, 0.51)),
        # Padded wider than the photo: capped at the whole width (the only non-square case).
        ((0.0, 0.45, 1.0, 0.55), (1000, 2000), (0.0, 0.2, 1.0, 0.8)),
    ],
)
def test_review_crop_region_is_square_in_pixels(
    box: tuple[float, float, float, float],
    size: tuple[int | None, int | None],
    expected: tuple[float, ...],
) -> None:
    region = crop_region(*box, *size)
    assert region == pytest.approx(expected)
    width, height = size
    if width and height and expected[2] - expected[0] < 1:
        wide, high = _square_px(region, width, height)
        assert wide == pytest.approx(high)


def test_portrait_crop_css_shows_the_box_unstretched() -> None:
    region = crop_region(0.4, 0.4, 0.6, 0.6, 1500, 2000)
    crop = Crop(1, 2, 3, 3, *region)
    # The image is drawn 1/0.32 x 1/0.24 of the square element: 312.5 % x 416.67 %, i.e. in
    # its own 1500:2000 proportions (312.5 / 416.67 = 0.75).
    assert crop.background_size == "312.50% 416.67%"
    assert crop.background_position == "50.00% 50.00%"

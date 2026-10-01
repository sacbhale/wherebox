"""apply.normalize_predictions: what a capture would store (shared with model comparisons)."""

from app.ai.schemas import ExtractedItem, ExtractResult, Usage
from app.capture.apply import MAX_NAME_CHARS, normalize_predictions


def _result(*names: str) -> ExtractResult:
    items = [
        ExtractedItem(name=name, quantity=2, kind="consumable", confidence=0.8, price_cents=199)
        for name in names
    ]
    return ExtractResult(items=items, usage=Usage())


def test_receipt_drops_denylisted_lines_and_cleans_names() -> None:
    result = _result("  Oat   milk\n1L ", "Sales TAX", "Paper bag", "Subtotal", "Bottle deposit")
    items = normalize_predictions("receipt", result)
    assert [item.name for item in items] == ["Oat milk 1L"]
    assert items[0].quantity == 2 and items[0].price_cents == 199
    assert result.items[0].name == "  Oat   milk\n1L "  # the result is not modified


def test_other_modes_keep_every_item() -> None:
    result = _result("Tax forms folder", "Gift  bag")
    for mode in ("shelf", "text", "reconcile"):
        assert [i.name for i in normalize_predictions(mode, result)] == [
            "Tax forms folder",
            "Gift bag",
        ]


def test_long_names_are_truncated() -> None:
    (item,) = normalize_predictions("shelf", _result("x" * 500))
    assert item.name == "x" * MAX_NAME_CHARS


def test_empty_result() -> None:
    assert normalize_predictions("receipt", _result()) == []

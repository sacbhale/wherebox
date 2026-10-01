"""Comparison scoring (app/compare/scoring.py): normalization, similarity on realistic
pairs, one-to-one matching, per-case scores, per-model aggregates, ranking and best value."""

import pytest

from app.compare.scoring import (
    Expected,
    Predicted,
    ResultScore,
    indicative,
    match,
    normalize,
    score_case,
    similarity,
    singular,
    summarize,
)


@pytest.mark.parametrize(
    ("word", "expected"),
    [
        ("eggs", "egg"),
        ("milks", "milk"),
        ("berries", "berry"),
        ("pies", "pie"),
        ("glasses", "glass"),
        ("boxes", "box"),
        ("matches", "match"),
        ("dishes", "dish"),
        ("glass", "glass"),
        ("gas", "gas"),  # under 4 characters: kept
        ("hummus", "hummus"),
        ("tennis", "tennis"),
        ("cases", "case"),
    ],
)
def test_singular(word: str, expected: str) -> None:
    assert singular(word) == expected


def test_normalize() -> None:
    assert normalize("  Coca-Cola  ZERO, 330ml cans! ") == "coca cola zero 330ml can"
    assert normalize("AA Batteries") == "aa battery"
    assert normalize("WD-40") == "wd 40"
    assert normalize("___") == ""


# (expected name, predicted name, should match at the 0.75 threshold)
PAIRS = [
    ("Milk", "milk", True),
    ("Milk", "Milks", True),
    ("Eggs", "egg", True),
    ("milk", "silk", False),  # short names need an exact normalized match
    ("Milk", "Oat milk", True),  # ... unless it is a whole word of the other name
    ("Eggs", "Free-range eggs", True),
    ("Tape", "Duct tape", True),
    ("Tape", "Tapenade", False),  # a prefix is not a word
    ("Soap", "Dish soap bottle", True),
    ("Oil", "Foil", False),
    ("Coca-Cola Zero", "Coca-Cola Zero 330ml can", True),  # containment
    ("Coke Zero", "Coca-Cola Zero 330ml can", False),  # "coke" is not contained
    ("Whole milk 1L", "Whole milk", True),
    ("AA batteries", "AA battery pack", True),
    ("Claw hammer", "Hammer", True),  # containment
    ("Screwdriver set", "Screwdriver", True),
    ("Paint brush", "Paintbrush", True),  # difflib ratio
    ("Dish soap", "Sourdough bread", False),
    ("Light bulbs", "Light bulb", True),
    ("Wood screws", "Wood glue", False),
    ("Extension cord", "Extension lead", True),  # difflib ratio 0.79: long shared word
    ("Chicken breast", "Chicken thighs", False),
    ("Apple juice", "Orange juice", False),
    ("Red wine", "Red onion", False),
]


@pytest.mark.parametrize(("expected", "predicted", "matches"), PAIRS)
def test_similarity_pairs(expected: str, predicted: str, matches: bool) -> None:
    assert (similarity(expected, predicted) >= 0.75) is matches
    assert similarity(expected, predicted) == similarity(predicted, expected)


def test_similarity_bounds() -> None:
    assert similarity("Milk", "MILK!") == 1.0
    assert similarity("", "milk") == 0.0
    assert 0.0 <= similarity("drill", "cordless drill") <= 1.0


def test_match_is_one_to_one_and_greedy_by_best() -> None:
    expected = [Expected("Milk"), Expected("Whole milk 1L")]
    predicted = [Predicted("Whole milk"), Predicted("milk"), Predicted("milk")]
    pairs = match(expected, predicted)
    assert sorted((e, p) for e, p, _ in pairs) == [(0, 1), (1, 0)]


def test_duplicates_count_once() -> None:
    expected = [Expected("Eggs", 12)]
    predicted = [Predicted("Eggs", 6), Predicted("eggs", 6)]
    scores = score_case(expected, predicted)
    assert scores["matched"] == 1
    assert scores["recall"] == 1.0 and scores["precision"] == 0.5
    assert scores["f1"] == pytest.approx(2 / 3)
    assert scores["quantity_accuracy"] == 0.0
    assert scores["extra"] == [1] and scores["missed"] == []


def test_score_case_perfect_and_partial() -> None:
    expected = [Expected("Milk", 2), Expected("Cordless drill"), Expected("Duct tape")]
    perfect = score_case(expected, [Predicted("milk", 2), Predicted("drill, cordless"),
                                    Predicted("duct tape")])  # fmt: skip
    assert perfect["f1"] == 1.0 and perfect["quantity_accuracy"] == 1.0
    assert perfect["price_accuracy"] is None
    partial = score_case(expected, [Predicted("Milk", 1), Predicted("Hammer")])
    assert partial["matched"] == 1
    assert partial["recall"] == pytest.approx(1 / 3) and partial["precision"] == 0.5
    assert partial["f1"] == pytest.approx(0.4)
    assert partial["quantity_accuracy"] == 0.0
    assert partial["pairs"] == [[0, 0]] and partial["missed"] == [1, 2] and partial["extra"] == [1]


def test_empty_sets() -> None:
    both = score_case([], [])
    assert both["f1"] == 1.0 and both["quantity_accuracy"] is None
    nothing_found = score_case([Expected("Milk")], [])
    assert nothing_found["f1"] == 0.0 and nothing_found["precision"] == 0.0
    nothing_expected = score_case([], [Predicted("Milk")])
    assert nothing_expected["f1"] == 0.0 and nothing_expected["extra"] == [0]


def test_receipt_price_accuracy_within_one_cent() -> None:
    expected = [
        Expected("Whole milk", 2, 258),
        Expected("Sourdough bread", 1, 399),
        Expected("Dish soap", 1, None),
    ]
    predicted = [
        Predicted("Whole milk", 2, 259),
        Predicted("Sourdough bread", 1, 349),
        Predicted("Dish soap", 1, 329),
    ]
    scores = score_case(expected, predicted, receipt=True)
    assert scores["price_accuracy"] == 0.5  # only priced expectations count
    assert score_case(expected, predicted)["price_accuracy"] is None


def _ok(model: str, f1: float, cost: int, duration: int = 1000, matched: int = 1) -> ResultScore:
    scores = {
        "expected": 2,
        "predicted": 2,
        "matched": matched,
        "f1": f1,
        "quantity_accuracy": 1.0,
        "price_accuracy": None,
    }
    return ResultScore(model, "ok", scores, cost, duration, 2)


def test_summarize_ranks_by_f1_then_cost_and_counts_failures() -> None:
    results = [
        _ok("a", 1.0, 300, 900),
        _ok("a", 0.5, 300, 1100),
        _ok("b", 1.0, 100, 2000),
        ResultScore("b", "failed", None, 50, 500, 2),
        _ok("c", 0.75, 10),
        _ok("c", 0.75, 10),
        ResultScore("c", "pending", None, 0, None, 2),
        ResultScore("c", "cancelled", None, 5, None, 2),
    ]
    summaries = summarize(["a", "b", "c"], results)
    # a and c tie on mean F1 0.75: the cheaper c ranks first.
    assert [s.model for s in summaries] == ["c", "a", "b"]
    c, a, b = summaries
    assert (a.rank, a.mean_f1, a.n, a.failures) == (2, 0.75, 2, 0)
    assert a.median_duration_ms == 1000 and a.total_cost_micros == 600
    assert a.cost_per_case_micros == 300
    assert (b.mean_f1, b.failures, b.n, b.missed) == (0.5, 1, 2, 3)  # failed: F1 0, all missed
    assert c.total_cost_micros == 25 and c.n == 2  # pending/cancelled add only cost
    # Best value: the cheapest within 0.05 of the best mean F1 (0.75): c.
    assert [s.best_value for s in summaries] == [True, False, False]
    assert indicative(summaries)


def test_one_cost_basis_ignores_unscored_spend() -> None:
    """Ranking ties and best value both use the spend of scored results per scored case,
    so money spent on attempts that were cancelled does not skew them."""
    results = [
        _ok("x", 0.8, 100),
        _ok("x", 0.8, 100),
        ResultScore("x", "cancelled", None, 1000, None, 2),
        _ok("y", 0.8, 150),
        _ok("y", 0.8, 150),
    ]
    x, y = summarize(["y", "x"], results)
    assert (x.model, x.rank, x.best_value) == ("x", 1, True)
    assert (x.cost_per_case_micros, x.scored_cost_micros, x.total_cost_micros) == (100, 200, 1200)
    assert (y.model, y.cost_per_case_micros) == ("y", 150)


def test_ranking_ties_break_on_lower_cost() -> None:
    summaries = summarize(["x", "y"], [_ok("x", 0.8, 500), _ok("y", 0.8, 200)])
    assert [s.model for s in summaries] == ["y", "x"]
    assert summaries[0].best_value


def test_best_value_needs_f1_within_margin() -> None:
    summaries = summarize(["good", "cheap"], [_ok("good", 0.9, 1000), _ok("cheap", 0.8, 1)])
    assert [s.model for s in summaries if s.best_value] == ["good"]
    near = summarize(["good", "cheap"], [_ok("good", 0.9, 1000), _ok("cheap", 0.86, 1)])
    assert [s.model for s in near if s.best_value] == ["cheap"]


def test_indicative_flag_needs_ten_cases() -> None:
    many = [_ok(m, 1.0, 1) for m in ("a", "b") for _ in range(10)]
    assert not indicative(summarize(["a", "b"], many))
    assert indicative(summarize(["a", "b"], many[:-1]))


def test_no_scored_results() -> None:
    summaries = summarize(["a", "b"], [])
    assert all(s.n == 0 and not s.best_value for s in summaries)
    assert summaries[0].cost_per_case_micros is None

"""Scoring model comparison results against a case's expected items (pure functions).

Names are normalized (casefold, punctuation to spaces, whitespace collapsed, simple plurals
made singular) and compared with ``similarity``: the larger of the difflib ratio and token
containment (every token of one name appears in the other, scored ``CONTAINMENT_SCORE``).
Names of at most ``SHORT_NAME_CHARS`` characters only match exactly or as a whole word of the
other name, so "milk" never matches "silk" but "Eggs" matches "Free-range eggs" and "Tape"
matches "Duct tape" (and "Milk" matches "Oat milk"). Examples of the resulting behaviour:

- "Eggs" / "egg": match (plural rule). "Boxes" / "box", "Berries" / "berry": match.
- "Coca-Cola Zero" / "Coca-Cola Zero 330ml can": match (containment).
- "Coke Zero" / "Coca-Cola Zero 330ml can": no match ("coke" is not a token of the other,
  and the difflib ratio is below the threshold) — models are expected to name what they see.

Predicted items are matched one-to-one to expected items, greedily by best similarity (ties:
larger token overlap, then list order), keeping pairs at or above ``THRESHOLD``.
"""

import difflib
import re
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

THRESHOLD = 0.75
SHORT_NAME_CHARS = 4
CONTAINMENT_SCORE = 0.9
# Two models whose mean F1 differ by at most this are "as good"; the cheaper is best value.
BEST_VALUE_MARGIN = 0.05
# Below this many scored cases the best-value flag is only indicative.
INDICATIVE_BELOW = 10
PRICE_TOLERANCE_CENTS = 1

_NON_WORD = re.compile(r"[\W_]+")


def singular(word: str) -> str:
    """Simple plural -> singular: ``ies``->``y`` (berries), ``sses``->``ss`` (glasses),
    ``xes``/``ches``/``shes`` drop ``es`` (boxes, matches, dishes), else a trailing ``s`` is
    dropped from words of 4+ characters not ending in ``ss``/``us``/``is`` (eggs, milks)."""
    if len(word) >= 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith("sses"):
        return word[:-2]
    if len(word) >= 4 and word.endswith(("xes", "ches", "shes")):
        return word[:-2]
    if len(word) >= 4 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def normalize(name: str) -> str:
    words = _NON_WORD.sub(" ", name.casefold()).split()
    return " ".join(singular(word) for word in words)


def _tokens(normalized: str) -> set[str]:
    return set(normalized.split())


def similarity(a: str, b: str) -> float:
    """Similarity of two names in [0, 1] (see the module docstring)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = _tokens(na), _tokens(nb)
    contained = ta <= tb or tb <= ta
    if min(len(na), len(nb)) <= SHORT_NAME_CHARS:
        return CONTAINMENT_SCORE if contained else 0.0
    # SequenceMatcher is not symmetric; take the better order so similarity(a, b) == (b, a).
    ratio = max(
        difflib.SequenceMatcher(None, na, nb).ratio(),
        difflib.SequenceMatcher(None, nb, na).ratio(),
    )
    return max(ratio, CONTAINMENT_SCORE if contained else 0.0)


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(normalize(a)), _tokens(normalize(b))
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


@dataclass(frozen=True)
class Expected:
    name: str
    quantity: int = 1
    price_cents: int | None = None


@dataclass(frozen=True)
class Predicted:
    name: str
    quantity: int = 1
    price_cents: int | None = None


def match(
    expected: Sequence[Expected], predicted: Sequence[Predicted], threshold: float = THRESHOLD
) -> list[tuple[int, int, float]]:
    """One-to-one (expected index, predicted index, similarity) pairs, best first."""
    candidates = []
    for ei, exp in enumerate(expected):
        for pi, pred in enumerate(predicted):
            score = similarity(exp.name, pred.name)
            if score >= threshold:
                candidates.append((score, _overlap(exp.name, pred.name), -ei, -pi))
    candidates.sort(reverse=True)
    used_e: set[int] = set()
    used_p: set[int] = set()
    pairs = []
    for score, _, neg_e, neg_p in candidates:
        ei, pi = -neg_e, -neg_p
        if ei in used_e or pi in used_p:
            continue
        used_e.add(ei)
        used_p.add(pi)
        pairs.append((ei, pi, score))
    return pairs


def _price_close(expected: Expected, predicted: Predicted) -> bool:
    if expected.price_cents is None or predicted.price_cents is None:
        return False
    return abs(predicted.price_cents - expected.price_cents) <= PRICE_TOLERANCE_CENTS


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def score_case(
    expected: Sequence[Expected], predicted: Sequence[Predicted], *, receipt: bool = False
) -> dict[str, Any]:
    """The score of one result, JSON-ready: recall, precision, f1, quantity accuracy
    (matched items with the exact quantity / matched), and for receipts price accuracy
    (matched items with an expected price within 1 cent / those with an expected price).
    Accuracies are None when there is nothing to measure. Also the matching itself
    (``pairs`` of [expected index, predicted index], ``missed`` and ``extra`` indexes)."""
    pairs = match(expected, predicted)
    matched = len(pairs)
    if not expected and not predicted:
        recall = precision = 1.0
    else:
        recall = _ratio(matched, len(expected)) if expected else 0.0
        precision = _ratio(matched, len(predicted)) if predicted else 0.0
    assert recall is not None and precision is not None
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    qty_ok = sum(1 for ei, pi, _ in pairs if expected[ei].quantity == predicted[pi].quantity)
    priced = [(ei, pi) for ei, pi, _ in pairs if expected[ei].price_cents is not None]
    price_ok = sum(1 for ei, pi in priced if _price_close(expected[ei], predicted[pi]))
    matched_e = {ei for ei, _, _ in pairs}
    matched_p = {pi for _, pi, _ in pairs}
    return {
        "expected": len(expected),
        "predicted": len(predicted),
        "matched": matched,
        "recall": recall,
        "precision": precision,
        "f1": f1,
        "quantity_accuracy": _ratio(qty_ok, matched),
        "price_accuracy": _ratio(price_ok, len(priced)) if receipt else None,
        "pairs": sorted([ei, pi] for ei, pi, _ in pairs),
        "missed": [i for i in range(len(expected)) if i not in matched_e],
        "extra": [i for i in range(len(predicted)) if i not in matched_p],
    }


# ---------------------------------------------------------------- per model


@dataclass(frozen=True)
class ResultScore:
    """What aggregation needs from one comparison result. ``status`` is ok, failed, pending
    or cancelled; ``scores`` is ``score_case`` output for ok results."""

    model: str
    status: str
    scores: dict[str, Any] | None
    cost_micros: int = 0
    duration_ms: int | None = None
    expected: int = 0  # expected item count of the case (counts as missed when failed)


@dataclass
class ModelSummary:
    model: str
    n: int = 0  # scored cases (ok + failed)
    ok: int = 0
    failures: int = 0
    mean_f1: float = 0.0
    found: int = 0
    missed: int = 0
    extra: int = 0
    quantity_accuracy: float | None = None
    price_accuracy: float | None = None
    total_cost_micros: int = 0  # everything spent, cancelled and pending attempts included
    scored_cost_micros: int = 0  # spent on the scored (ok + failed) results
    median_duration_ms: float | None = None
    rank: int = 0
    best_value: bool = False
    durations: list[int] = field(default_factory=list, repr=False)

    @property
    def cost_per_case_micros(self) -> float | None:
        """Spend of the scored results per scored case: the one cost basis for ranking ties
        and best value (a cancelled run does not skew it)."""
        return self.scored_cost_micros / self.n if self.n else None


def _mean(values: Iterable[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def summarize(models: Sequence[str], results: Iterable[ResultScore]) -> list[ModelSummary]:
    """Per-model aggregates, ranked: mean F1 (failed results count as 0) descending, ties by
    lower cost per scored case. Pending and cancelled results only add to the total cost."""
    by_model = {model: ModelSummary(model) for model in models}
    f1s: dict[str, list[float]] = {model: [] for model in models}
    qty: dict[str, list[float | None]] = {model: [] for model in models}
    price: dict[str, list[float | None]] = {model: [] for model in models}
    for result in results:
        summary = by_model.get(result.model)
        if summary is None:
            continue
        summary.total_cost_micros += result.cost_micros
        if result.status == "ok" and result.scores is not None:
            s = result.scores
            summary.n += 1
            summary.ok += 1
            summary.found += s["matched"]
            summary.missed += s["expected"] - s["matched"]
            summary.extra += s["predicted"] - s["matched"]
            f1s[result.model].append(s["f1"])
            qty[result.model].append(s["quantity_accuracy"])
            price[result.model].append(s["price_accuracy"])
        elif result.status == "failed":
            summary.n += 1
            summary.failures += 1
            summary.missed += result.expected
            f1s[result.model].append(0.0)
        else:
            continue
        summary.scored_cost_micros += result.cost_micros
        if result.duration_ms is not None:
            summary.durations.append(result.duration_ms)
    for model, summary in by_model.items():
        summary.mean_f1 = _mean(f1s[model]) or 0.0
        summary.quantity_accuracy = _mean(qty[model])
        summary.price_accuracy = _mean(price[model])
        summary.median_duration_ms = (
            statistics.median(summary.durations) if summary.durations else None
        )
    ranked = sorted(by_model.values(), key=lambda s: (-s.mean_f1, s.cost_per_case_micros or 0.0))
    for position, summary in enumerate(ranked, start=1):
        summary.rank = position
    mark_best_value(ranked)
    return ranked


def mark_best_value(summaries: Sequence[ModelSummary]) -> ModelSummary | None:
    """Flag the cheapest model (per case) whose mean F1 is within BEST_VALUE_MARGIN of the
    best; only models with scored cases qualify. Returns it, or None."""
    scored = [s for s in summaries if s.n]
    if not scored:
        return None
    best_f1 = max(s.mean_f1 for s in scored)
    contenders = [s for s in scored if s.mean_f1 >= best_f1 - BEST_VALUE_MARGIN - 1e-9]
    choice = min(contenders, key=lambda s: (s.cost_per_case_micros or 0.0, -s.mean_f1))
    for summary in summaries:
        summary.best_value = summary is choice
    return choice


def indicative(summaries: Sequence[ModelSummary]) -> bool:
    """True when the best-value flag rests on fewer than INDICATIVE_BELOW cases."""
    return min((s.n for s in summaries), default=0) < INDICATIVE_BELOW

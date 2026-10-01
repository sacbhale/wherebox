"""Checks before a comparison run starts: availability, model and case selection, the rough
cost estimate and the budget refusal (read-only; ``runs.start`` does the write).

Comparisons are available with OPENROUTER_API_KEY whatever AI_PROVIDER is (an Anthropic user
can try OpenRouter models before switching), and under the test-only fake provider with
OPENROUTER_CATALOG_FILE.
"""

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.ai import openrouter_catalog
from app.ai.openrouter_catalog import ModelInfo
from app.capture.budget import budget_micros, month_usage, token_estimate
from app.compare import cases
from app.compare.cases import Case
from app.config import Settings

MIN_MODELS = 2
MAX_MODELS = 5
MAX_CASES = 25
# The run is refused when spent + BUDGET_FACTOR x estimate would exceed the budget: the
# estimate is rough, and the per-call budget re-check bounds the real spend.
BUDGET_FACTOR = 2
UNAVAILABLE = (
    "Model comparison needs an OpenRouter key: set OPENROUTER_API_KEY in .env "
    "(AI_PROVIDER can stay as it is)."
)


def available(settings: Settings) -> bool:
    return settings.openrouter_api_key is not None or (
        settings.ai_provider == "fake" and settings.openrouter_catalog_file is not None
    )


class PlanError(ValueError):
    """The run cannot start; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.status = status


def validate_models(settings: Settings, model_ids: Sequence[str]) -> list[ModelInfo]:
    """2-5 distinct, well-formed ids of priced, non-router models present in the catalog."""
    ids = list(dict.fromkeys(m.strip() for m in model_ids if m.strip()))
    for model_id in ids:
        if not openrouter_catalog.valid_model_id(model_id):
            raise PlanError(f"{model_id!r} is not an OpenRouter model id.", 400)
    if not MIN_MODELS <= len(ids) <= MAX_MODELS:
        raise PlanError(f"Pick {MIN_MODELS} to {MAX_MODELS} different models.")
    catalog = openrouter_catalog.load_catalog(settings)
    if not catalog.available:
        raise PlanError("The OpenRouter model list is unavailable right now; try again later.")
    models = []
    for model_id in ids:
        info = catalog.get(model_id)
        if info is None:
            raise PlanError(f"Model {model_id} is not in the OpenRouter model list.", 400)
        if info.is_router:
            raise PlanError(f"{model_id} routes to varying models and cannot be compared.", 400)
        if not info.price_known:
            raise PlanError(f"Model {model_id} has no published price.", 400)
        models.append(info)
    return models


def select_cases(
    conn: sqlite3.Connection, settings: Settings, case_ids: Sequence[int] | None
) -> list[Case]:
    """The chosen usable cases (None: all usable, newest first, at most MAX_CASES)."""
    usable = [case for case in cases.live(conn, settings) if case.usable]
    if case_ids is None:
        chosen = usable[:MAX_CASES]
    else:
        wanted = set(case_ids)
        chosen = [case for case in usable if case.id in wanted]
        if len(wanted) > MAX_CASES:
            raise PlanError(f"Pick at most {MAX_CASES} cases.")
        if len(chosen) != len(wanted):
            raise PlanError("Some chosen cases are deleted or their input is missing.", 400)
    if not chosen:
        raise PlanError("There are no usable comparison cases.")
    return chosen


@dataclass(frozen=True)
class Estimate:
    tokens: tuple[int, int, bool]  # per call (average over the cases): input, output, history
    per_model_micros: dict[str, int]  # whole run, per model
    total_micros: int


def estimate(
    conn: sqlite3.Connection, models: Sequence[ModelInfo], image_counts: Sequence[int]
) -> Estimate:
    """Rough cost of one call per case (``image_counts``: photos each case sends) for each
    model, from the image-aware per-call estimate (``budget.token_estimate``)."""
    per_case = [token_estimate(conn, images) for images in image_counts]
    per_model = {
        m.id: round(sum((m.cost_usd(t[0], t[1]) or 0.0) for t in per_case) * 1_000_000)
        for m in models
    }
    if per_case:
        tokens = (
            round(sum(t[0] for t in per_case) / len(per_case)),
            round(sum(t[1] for t in per_case) / len(per_case)),
            all(t[2] for t in per_case),
        )
    else:
        tokens = token_estimate(conn, 1)
    return Estimate(tokens, per_model, sum(per_model.values()))


def budget_refusal(
    conn: sqlite3.Connection, settings: Settings, estimate_micros: int, now: datetime
) -> str | None:
    limit = budget_micros(settings)
    if limit is None:
        return None
    spent = month_usage(conn, settings, now).cost_micros
    if spent + BUDGET_FACTOR * estimate_micros > limit:
        remaining = max(limit - spent, 0) / 1_000_000
        return (
            f"This comparison could cost up to ${BUDGET_FACTOR * estimate_micros / 1e6:.4f} "
            f"(twice the rough estimate) and only ${remaining:.4f} of this month's AI budget "
            "is left. Pick fewer models or cases, or cheaper models."
        )
    return None


@dataclass(frozen=True)
class Plan:
    models: list[ModelInfo]
    cases: list[Case]
    estimate: Estimate


def plan(
    conn: sqlite3.Connection,
    settings: Settings,
    model_ids: Sequence[str],
    case_ids: Sequence[int] | None,
    now: datetime,
) -> Plan:
    """Everything a run needs, or PlanError with the reason it cannot start."""
    if not available(settings):
        raise PlanError(UNAVAILABLE, 404)
    models = validate_models(settings, model_ids)
    chosen = select_cases(conn, settings, case_ids)
    rough = estimate(conn, models, [case.image_count for case in chosen])
    refusal = budget_refusal(conn, settings, rough.total_micros, now)
    if refusal:
        raise PlanError(refusal)
    return Plan(models, chosen, rough)

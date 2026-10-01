"""The OpenRouter model catalog (``GET /api/v1/models``, public, no key needed).

The server fetches it (10 s timeout) and keeps it in memory for an hour, guarded by a lock so
concurrent callers fetch once. The last good copy is persisted to
``DATA_DIR/cache/openrouter_models.json``, so a restart or an outage still has a list; after
a failed fetch the next attempt waits ``RETRY_AFTER_FAILURE_S``. A refresh (at most hourly)
blocks its caller for up to ``FETCH_TIMEOUT_S``. ``OPENROUTER_CATALOG_FILE`` (test-only)
replaces the fetch with a local file, and is honoured only when AI_PROVIDER=fake.

Entries whose price is not a non-negative number (e.g. ``openrouter/auto`` reports ``-1``)
are "price unknown": ``get_model`` still returns them, the pickers (``list_models``) do not.
"""

import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx

from app.config import Settings

log = logging.getLogger(__name__)

MODELS_URL = "https://openrouter.ai/api/v1/models"
FETCH_TIMEOUT_S = 10.0
CACHE_TTL_S = 3600.0
RETRY_AFTER_FAILURE_S = 300.0
CACHE_FILE = "openrouter_models.json"
# vendor/model[:variant], matched in full; a leading "~" marks OpenRouter's "latest" aliases
# (e.g. "~google/gemini-flash-latest"). Checked against every id in the live catalog.
MODEL_ID = re.compile(r"~?[A-Za-z0-9._-]+/[A-Za-z0-9._:~-]+")
ROUTER_PREFIX = "openrouter/"
# OpenRouter reasoning efforts, least to most (docs: "Reasoning Tokens").
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

Strategy = Literal["tools", "json_schema", "prompt"]
Source = Literal["live", "cache", "file", "none"]


def valid_model_id(model_id: str | None) -> bool:
    """True for a syntactically valid OpenRouter model id (checked before any use)."""
    if not isinstance(model_id, str) or len(model_id) > 200:
        return False
    return MODEL_ID.fullmatch(model_id) is not None


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    context_length: int | None
    prompt_usd_per_mtok: float | None  # None: price unknown
    completion_usd_per_mtok: float | None
    accepts_images: bool
    strategy: Strategy
    # The model lists ``parallel_tool_calls`` (the adapter only sends it then, because
    # ``provider.require_parameters`` would otherwise leave no provider to route to).
    parallel_tool_calls: bool = False
    # Reasoning is on by default (or mandatory) above "low" effort and "low" is accepted:
    # the adapter asks for ``reasoning: {"effort": "low"}`` to keep the output within the cap.
    lower_reasoning: bool = False

    @property
    def price_known(self) -> bool:
        return self.prompt_usd_per_mtok is not None and self.completion_usd_per_mtok is not None

    @property
    def is_router(self) -> bool:
        """OpenRouter's routing pseudo-models (``openrouter/auto``, ``openrouter/free``...):
        the model that answers varies per call, so they are excluded from comparisons."""
        return self.id.startswith(ROUTER_PREFIX)

    @property
    def is_free(self) -> bool:
        return self.prompt_usd_per_mtok == 0 and self.completion_usd_per_mtok == 0

    def cost_usd(self, input_tokens: int, output_tokens: int) -> float | None:
        """Estimated USD cost of a call with these token counts (None: price unknown)."""
        if self.prompt_usd_per_mtok is None or self.completion_usd_per_mtok is None:
            return None
        return (
            input_tokens * self.prompt_usd_per_mtok + output_tokens * self.completion_usd_per_mtok
        ) / 1_000_000


@dataclass(frozen=True)
class Catalog:
    """A catalog snapshot. ``fetched_at`` is when its data was fetched (None if unknown);
    ``error`` is the last fetch failure when this is a fallback copy."""

    models: tuple[ModelInfo, ...] = ()
    source: Source = "none"
    fetched_at: datetime | None = None
    error: str | None = None
    _by_id: dict[str, ModelInfo] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._by_id.update({model.id: model for model in self.models})

    def get(self, model_id: str) -> ModelInfo | None:
        return self._by_id.get(model_id)

    @property
    def available(self) -> bool:
        return bool(self.models)

    @property
    def fresh(self) -> bool:
        """True when the list comes from a successful fetch (or the test file), so a model
        missing from it is really gone."""
        return self.source in ("live", "file")


def _price_per_mtok(value: Any) -> float | None:
    try:
        per_token = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(per_token) or per_token < 0:
        return None
    return per_token * 1_000_000


def _strategy(parameters: set[str]) -> Strategy:
    if {"tools", "tool_choice"} <= parameters:
        return "tools"
    if parameters & {"structured_outputs", "response_format"}:
        return "json_schema"
    return "prompt"


def _lower_reasoning(reasoning: Any, parameters: set[str]) -> bool:
    """True when asking for "low" reasoning effort lowers this model's default (see
    ``ModelInfo.lower_reasoning``). ``supported_efforts`` null means every effort is
    accepted; omitted means the model has no effort selection."""
    if "reasoning" not in parameters or not isinstance(reasoning, dict):
        return False
    if not (reasoning.get("mandatory") is True or reasoning.get("default_enabled") is True):
        return False
    if "supported_efforts" not in reasoning:
        return False
    efforts = reasoning["supported_efforts"]
    if efforts is not None and (not isinstance(efforts, list) or "low" not in efforts):
        return False
    default = reasoning.get("default_effort")
    return default in EFFORTS and EFFORTS.index(default) > EFFORTS.index("low")


def parse_model(entry: Any) -> ModelInfo | None:
    """One ``data[]`` entry, or None when it has no valid id."""
    if not isinstance(entry, dict) or not valid_model_id(entry.get("id")):
        return None
    architecture = entry.get("architecture") or {}
    pricing = entry.get("pricing") or {}
    parameters = {str(p) for p in entry.get("supported_parameters") or []}
    context = entry.get("context_length")
    return ModelInfo(
        id=entry["id"],
        name=str(entry.get("name") or entry["id"]),
        context_length=context if isinstance(context, int) else None,
        prompt_usd_per_mtok=_price_per_mtok(pricing.get("prompt")),
        completion_usd_per_mtok=_price_per_mtok(pricing.get("completion")),
        accepts_images="image" in (architecture.get("input_modalities") or []),
        strategy=_strategy(parameters),
        parallel_tool_calls="parallel_tool_calls" in parameters,
        lower_reasoning=_lower_reasoning(entry.get("reasoning"), parameters),
    )


def parse_catalog(data: Any) -> tuple[ModelInfo, ...]:
    """All valid entries of a ``/api/v1/models`` response. Raises ValueError when the
    response is not of that shape."""
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        raise ValueError("unexpected /models response shape")
    parsed = (parse_model(entry) for entry in data["data"])
    return tuple(model for model in parsed if model is not None)


def fetch_models() -> Any:
    """GET the public models endpoint (no credentials are sent)."""
    response = httpx.get(MODELS_URL, timeout=FETCH_TIMEOUT_S)
    response.raise_for_status()
    return response.json()


@dataclass
class _State:
    catalog: Catalog | None = None
    loaded_at: float = 0.0  # monotonic time of the last successful fetch
    failed_at: float | None = None  # monotonic time of the last failed fetch


_lock = threading.Lock()
_states: dict[Path, _State] = {}


def clear_cache() -> None:
    """Forget every in-memory catalog (tests)."""
    with _lock:
        _states.clear()


def _cache_path(settings: Settings) -> Path:
    return settings.cache_dir / CACHE_FILE


def _persist(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.warning("could not persist the OpenRouter catalog to %s", path, exc_info=True)


def _read_persisted(path: Path, error: str) -> Catalog | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        models = parse_catalog(data)
        fetched_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    except (OSError, ValueError):
        return None
    return Catalog(models=models, source="cache", fetched_at=fetched_at, error=error)


def _read_file(path: Path) -> Catalog:
    try:
        models = parse_catalog(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        return Catalog(error=f"cannot read OPENROUTER_CATALOG_FILE: {exc}")
    return Catalog(models=models, source="file", fetched_at=None)


def load_catalog(
    settings: Settings,
    *,
    fetch: Callable[[], Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> Catalog:
    """The current catalog: memory (< 1 h old), else a fresh fetch, else the last good copy
    (memory, then the persisted file), else an empty catalog carrying the error."""
    if settings.openrouter_catalog_file is not None and settings.ai_provider == "fake":
        return _read_file(settings.openrouter_catalog_file)
    fetch = fetch or fetch_models
    path = _cache_path(settings)
    with _lock:
        state = _states.setdefault(path, _State())
        now = monotonic()
        catalog = state.catalog
        if catalog is not None and catalog.source == "live" and now - state.loaded_at < CACHE_TTL_S:
            return catalog
        failed_at = state.failed_at
        recently_failed = failed_at is not None and now - failed_at < RETRY_AFTER_FAILURE_S
        if catalog is not None and recently_failed:
            return catalog
        try:
            data = fetch()
            models = parse_catalog(data)
        except (httpx.HTTPError, ValueError) as exc:
            error = f"OpenRouter model list unavailable ({type(exc).__name__})"
            log.warning("%s: %s", error, exc)
            state.failed_at = now
            previous = state.catalog
            if previous is not None and previous.models:
                state.catalog = Catalog(
                    models=previous.models,
                    source="cache",
                    fetched_at=previous.fetched_at,
                    error=error,
                )
            else:
                state.catalog = _read_persisted(path, error) or Catalog(error=error)
            return state.catalog
        _persist(path, data)
        state.catalog = Catalog(models=models, source="live", fetched_at=datetime.now(UTC))
        state.loaded_at = now
        state.failed_at = None
        return state.catalog


def get_model(settings: Settings, model_id: str) -> ModelInfo | None:
    """The catalog entry for ``model_id`` (price-unknown entries included), or None when the
    id is invalid, unknown, or the catalog is unavailable."""
    if not valid_model_id(model_id):
        return None
    return load_catalog(settings).get(model_id)


def matches(model: ModelInfo, query: str) -> bool:
    """Every whitespace-separated term of ``query`` occurs in the id or name (casefolded)."""
    haystack = f"{model.id} {model.name}".casefold()
    return all(term in haystack for term in query.casefold().split())


def list_models(settings: Settings, query: str = "", images_only: bool = True) -> list[ModelInfo]:
    """Picker entries: priced models matching ``query``, image-capable ones only by default.
    Catalog order (newest first) is kept."""
    return [
        model
        for model in load_catalog(settings).models
        if model.price_known and (model.accepts_images or not images_only) and matches(model, query)
    ]

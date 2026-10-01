"""OpenRouter catalog: parsing a trimmed real response, filters, strategies, prices, the
memory cache with its lock, the persisted fallback, and model id validation."""

import json
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.ai import openrouter_catalog as catalog_mod
from app.ai.openrouter_catalog import (
    CACHE_TTL_S,
    RETRY_AFTER_FAILURE_S,
    list_models,
    load_catalog,
    parse_catalog,
    valid_model_id,
)
from app.config import Settings
from tests.conftest import SettingsFactory

FIXTURES = Path(__file__).parent.parent / "fixtures" / "openrouter"
MODELS = FIXTURES / "models.json"
RAW = json.loads(MODELS.read_text())


@pytest.fixture(autouse=True)
def _fresh_cache():
    catalog_mod.clear_cache()
    yield
    catalog_mod.clear_cache()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Fetch:
    """A scripted catalog fetch: returns ``data`` or raises ``error``; counts calls."""

    def __init__(self, data: Any = RAW, error: Exception | None = None, delay: float = 0) -> None:
        self.data = data
        self.error = error
        self.delay = delay
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.data


@pytest.fixture
def live(make_settings: SettingsFactory) -> Settings:
    """Settings that would fetch over HTTP (tests always pass ``fetch``)."""
    return make_settings(ai_provider="openrouter", openrouter_api_key="sk-or-test")


@pytest.fixture
def from_file(make_settings: SettingsFactory) -> Settings:
    return make_settings(ai_provider="fake", openrouter_catalog_file=MODELS)


# ---------------------------------------------------------------- ids


def test_every_real_model_id_matches_the_pattern() -> None:
    lines = (FIXTURES / "model_ids.txt").read_text().splitlines()
    ids = [line for line in lines if line and not line.startswith("#")]
    assert len(ids) > 400
    assert [model_id for model_id in ids if not valid_model_id(model_id)] == []
    assert all(valid_model_id(entry["id"]) for entry in RAW["data"])


@pytest.mark.parametrize(
    "model_id",
    [
        "claude-sonnet-5-5",  # the Anthropic default: no vendor prefix
        "",
        "google/",
        "/model",
        "a/b/c",
        "vendor/model name",
        "vendor/model?x=1",
        "../etc/passwd",
        "vendor/model\n",
        "<script>/x",
        "v/" + "m" * 300,
    ],
)
def test_invalid_model_ids(model_id: str) -> None:
    assert not valid_model_id(model_id)


def test_valid_model_ids() -> None:
    for model_id in ("openai/gpt-5-nano", "qwen/qwen3.8-27b:free", "~google/gemini-flash-latest"):
        assert valid_model_id(model_id)
    assert not valid_model_id(None)


# ---------------------------------------------------------------- parsing


def test_parse_fixture_strategies_images_and_prices() -> None:
    models = {model.id: model for model in parse_catalog(RAW)}
    assert len(models) == len(RAW["data"])
    lite = models["google/gemini-3.1-flash-lite"]
    assert lite.strategy == "tools" and lite.accepts_images
    assert lite.prompt_usd_per_mtok == pytest.approx(0.25)
    assert lite.completion_usd_per_mtok == pytest.approx(1.5)
    assert lite.context_length and lite.name.startswith("Google:")
    assert lite.cost_usd(1_500, 400) == pytest.approx((1_500 * 0.25 + 400 * 1.5) / 1e6)
    # structured_outputs only; response_format only; tools without tool_choice + JSON.
    assert models["google/gemini-2.5-flash-image"].strategy == "json_schema"
    assert models["google/gemini-3.1-flash-lite-image"].strategy == "json_schema"
    assert models["cohere/command-a-plus"].strategy == "json_schema"
    # Neither: prompt-only ("best effort").
    assert models["amazon/nova-lite-v1"].strategy == "prompt"
    assert models["perplexity/sonar"].strategy == "prompt"
    # Text-only model.
    assert not models["z-ai/glm-5.3-prime"].accepts_images
    # Free model.
    free = models["qwen/qwen3.8-27b:free"]
    assert free.is_free and free.price_known and free.cost_usd(1000, 1000) == 0
    # Router pseudo-models; openrouter/auto reports price -1 (unknown).
    assert models["openrouter/free"].is_router and models["openrouter/auto"].is_router
    assert not lite.is_router
    auto = models["openrouter/auto"]
    assert auto.prompt_usd_per_mtok is None and not auto.price_known
    assert auto.cost_usd(1, 1) is None
    # Synthesized non-numeric price.
    assert not models["test/price-unknown"].price_known
    # Capabilities the adapter only sends when listed.
    assert not lite.parallel_tool_calls


@pytest.mark.parametrize(
    ("model_id", "lower"),
    [
        ("anthropic/claude-sonnet-5.5", True),  # mandatory, default "high"
        ("openai/gpt-5-nano", True),  # mandatory, default "medium"
        ("google/gemini-3.1-flash-lite", False),  # default "minimal": "low" would raise it
        ("z-ai/glm-5.3-prime", True),  # default "max", "low" supported
        ("amazon/nova-lite-v1", False),  # no reasoning
    ],
)
def test_lower_reasoning_from_the_catalog(model_id: str, lower: bool) -> None:
    models = {model.id: model for model in parse_catalog(RAW)}
    assert models[model_id].lower_reasoning is lower


def test_lower_reasoning_rules() -> None:
    params = {"reasoning"}
    lower = catalog_mod._lower_reasoning
    on = {"default_enabled": True, "default_effort": "medium", "supported_efforts": None}
    assert lower(on, params)
    assert not lower(on, set())  # parameter not listed
    assert not lower(on | {"default_enabled": False}, params)  # would switch reasoning on
    assert not lower({k: v for k, v in on.items() if k != "supported_efforts"}, params)
    assert not lower(on | {"supported_efforts": ["high", "medium"]}, params)
    assert lower(on | {"supported_efforts": ["low", "high"]}, params)
    assert not lower(on | {"default_effort": "low"}, params)
    assert lower({"mandatory": True, "default_effort": "high", "supported_efforts": None}, params)


def test_parse_skips_invalid_entries_and_rejects_bad_shape() -> None:
    data = {"data": [{"id": "bad id"}, {"name": "no id"}, "junk", RAW["data"][0]]}
    assert [m.id for m in parse_catalog(data)] == [RAW["data"][0]["id"]]
    minimal = parse_catalog({"data": [{"id": "x/y"}]})[0]
    assert minimal.strategy == "prompt" and not minimal.accepts_images
    assert not minimal.price_known and minimal.context_length is None
    for bad in ([], {"data": "x"}, None):
        with pytest.raises(ValueError):
            parse_catalog(bad)


# ---------------------------------------------------------------- filters


def test_list_models_filters(from_file: Settings) -> None:
    ids = [m.id for m in list_models(from_file)]
    assert "google/gemini-3.1-flash-lite" in ids
    assert "z-ai/glm-5.3-prime" not in ids  # text-only hidden by default
    assert "openrouter/auto" not in ids and "test/price-unknown" not in ids  # price unknown
    everything = [m.id for m in list_models(from_file, images_only=False)]
    assert "z-ai/glm-5.3-prime" in everything and "test/text-only" in everything
    assert "openrouter/auto" not in everything
    assert set(ids) < set(everything)
    assert [m.id for m in list_models(from_file, query="GEMINI lite")] == [
        "google/gemini-3.1-flash-lite",
        "google/gemini-3.1-flash-lite-image",
    ]
    assert [m.id for m in list_models(from_file, query="model-b")] == ["test/model-b"]
    assert list_models(from_file, query="no such model") == []


def test_get_model(from_file: Settings) -> None:
    assert catalog_mod.get_model(from_file, "test/model-a").name == "Test: Model A"
    assert catalog_mod.get_model(from_file, "openrouter/auto") is not None  # price unknown
    assert catalog_mod.get_model(from_file, "vendor/missing") is None
    assert catalog_mod.get_model(from_file, "not an id") is None


def test_catalog_file_is_ignored_unless_the_provider_is_fake(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings(ai_provider="openrouter", openrouter_catalog_file=MODELS)
    fetch = Fetch(error=httpx.ConnectError("down"))
    catalog = load_catalog(settings, fetch=fetch, monotonic=Clock())
    assert fetch.calls == 1 and catalog.source == "none"


def test_catalog_file_errors_give_empty_catalog(make_settings: SettingsFactory, tmp_path) -> None:
    missing = make_settings(openrouter_catalog_file=tmp_path / "nope.json")
    catalog = load_catalog(missing)
    assert not catalog.available and "OPENROUTER_CATALOG_FILE" in (catalog.error or "")
    assert list_models(missing) == []


# ---------------------------------------------------------------- cache


def test_memory_cache_for_an_hour_and_persisted_copy(live: Settings) -> None:
    fetch, clock = Fetch(), Clock()
    first = load_catalog(live, fetch=fetch, monotonic=clock)
    assert first.source == "live" and first.available and first.fresh
    assert first.fetched_at is not None
    persisted = live.cache_dir / "openrouter_models.json"
    assert json.loads(persisted.read_text()) == RAW

    clock.now += CACHE_TTL_S - 1
    assert load_catalog(live, fetch=fetch, monotonic=clock) is first
    assert fetch.calls == 1
    clock.now += 2
    assert load_catalog(live, fetch=fetch, monotonic=clock) is not first
    assert fetch.calls == 2


def test_fetch_failure_keeps_the_last_good_list(live: Settings) -> None:
    clock = Clock()
    good = load_catalog(live, fetch=Fetch(), monotonic=clock)
    clock.now += CACHE_TTL_S + 1
    failing = Fetch(error=httpx.ConnectError("down"))
    stale = load_catalog(live, fetch=failing, monotonic=clock)
    assert stale.source == "cache" and not stale.fresh
    assert stale.models == good.models and stale.error
    # No new attempt until RETRY_AFTER_FAILURE_S has passed.
    clock.now += RETRY_AFTER_FAILURE_S - 1
    load_catalog(live, fetch=failing, monotonic=clock)
    assert failing.calls == 1
    clock.now += 2
    recovered = load_catalog(live, fetch=Fetch(), monotonic=clock)
    assert recovered.source == "live" and recovered.error is None


def test_restart_during_outage_uses_the_persisted_copy(live: Settings) -> None:
    load_catalog(live, fetch=Fetch(), monotonic=Clock())
    catalog_mod.clear_cache()  # a restart
    catalog = load_catalog(live, fetch=Fetch(error=httpx.ReadTimeout("slow")), monotonic=Clock())
    assert catalog.source == "cache" and catalog.available
    assert catalog.fetched_at is not None
    assert catalog.get("test/model-a") is not None


def test_no_copy_at_all_gives_an_empty_catalog(live: Settings) -> None:
    catalog = load_catalog(live, fetch=Fetch(error=ValueError("bad json")), monotonic=Clock())
    assert not catalog.available and catalog.source == "none" and catalog.error


def test_bad_response_shape_is_a_failure(live: Settings) -> None:
    catalog = load_catalog(live, fetch=Fetch(data={"unexpected": 1}), monotonic=Clock())
    assert not catalog.available and catalog.error
    assert not (live.cache_dir / "openrouter_models.json").exists()


def test_concurrent_callers_fetch_once(live: Settings) -> None:
    fetch = Fetch(delay=0.2)
    results: list[Any] = []
    threads = [
        threading.Thread(target=lambda: results.append(load_catalog(live, fetch=fetch)))
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert fetch.calls == 1
    assert len(results) == 5 and all(result is results[0] for result in results)


def test_fetch_models_uses_public_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[httpx.Request] = []

    def fake_get(url: str, *, timeout: float) -> httpx.Response:
        seen.append(httpx.Request("GET", url))
        assert timeout == catalog_mod.FETCH_TIMEOUT_S
        return httpx.Response(200, json=RAW, request=seen[-1])

    monkeypatch.setattr(catalog_mod.httpx, "get", fake_get)
    assert catalog_mod.fetch_models() == RAW
    assert str(seen[0].url) == "https://openrouter.ai/api/v1/models"
    assert "authorization" not in seen[0].headers

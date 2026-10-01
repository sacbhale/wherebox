"""Active AI model: the Settings picker (htmx), persistence, env fallback/reset, warnings,
validation, and the worker using (and recording) the chosen model on the next capture."""

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ai import openrouter_catalog
from app.ai.model_choice import effective_model, stored_model
from app.ai.provider import ProviderTimeoutError, ProviderTransientError
from app.ai.registry import make_provider_for
from app.ai.schemas import ExtractRequest, ExtractResult, Usage
from app.capture import model_setting, service
from app.capture.budget import record_ai_call
from app.capture.worker import Worker, run_once
from app.config import Settings
from app.db.migrate import migrate
from tests.capture_helpers import capture_id, capture_items, capture_row, post_text
from tests.conftest import TEST_PASSWORD, SettingsFactory, logged_in
from tests.inventory_helpers import HX, open_db

CATALOG = Path(__file__).parent.parent / "fixtures" / "openrouter" / "models.json"


def _step(name: str) -> dict:
    item = {"name": name, "quantity": 1, "kind": "consumable", "confidence": 0.9}
    return {"result": {"items": [item], "usage": {"input_tokens": 100, "output_tokens": 10}}}


@pytest.fixture(autouse=True)
def _fresh_catalog(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    # OPENROUTER_CATALOG_FILE only counts under the fake provider; with AI_PROVIDER=openrouter
    # the "fetch" returns the fixture (no network in tests).
    monkeypatch.setattr(openrouter_catalog, "fetch_models", lambda: json.loads(CATALOG.read_text()))
    openrouter_catalog.clear_cache()
    yield
    openrouter_catalog.clear_cache()


@pytest.fixture
def script(tmp_path: Path) -> Path:
    path = tmp_path / "script.json"
    path.write_text(
        json.dumps(
            {
                "steps": [_step("Default milk"), _step("Default eggs")],
                "models": {"test/model-b": {"steps": [_step("Model B milk")]}},
            }
        )
    )
    return path


@pytest.fixture
def auth_settings(make_settings: SettingsFactory, script: Path) -> Settings:
    """AI_PROVIDER=fake with the test-only catalog file: the picker is shown."""
    return make_settings(
        household_password=TEST_PASSWORD,
        openrouter_catalog_file=CATALOG,
        fake_ai_script=script,
        ai_model="test/model-a",
    )


def _choose(client: TestClient, model: str, **kwargs) -> object:
    return client.post("/settings/model", data={"model": model}, headers=HX, **kwargs)


def _tick(settings: Settings, worker: Worker) -> None:
    with open_db(settings) as conn:
        assert worker._tick(conn)


# ---------------------------------------------------------------- page


def test_settings_shows_picker_with_env_model(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    assert auth_settings.cache_dir.is_dir()  # created by the lifespan for the catalog copy
    html = auth_client.get("/settings").text
    assert 'id="ai-model"' in html
    assert "fake / test/model-a" in html
    assert "Using AI_MODEL from .env." in html
    assert 'id="ai-model-reset"' not in html
    assert "Test: Model A" in html and "$0.50 input / $2.00 output per 1M tokens" in html
    # Picker rows: images-only by default, priced models only, with badges and estimates.
    assert 'data-model-id="google/gemini-3.1-flash-lite"' in html
    assert 'data-model-id="test/text-only"' not in html
    assert 'data-model-id="openrouter/auto"' not in html  # price unknown
    assert 'data-model-id="test/price-unknown"' not in html
    assert "about 1,500 input +" in html and "400 output tokens (typical" in html
    # test/model-a: (1500 * 0.5 + 400 * 2) / 1e6 = $0.00155
    assert "~$0.00155 per capture (rough)" in html
    assert 'hx-trigger="input changed delay:250ms, search"' in html


def test_picker_search_and_all_models_toggle(auth_client: TestClient) -> None:
    response = auth_client.get("/settings/models", params={"q": "model-b"})
    assert response.status_code == 200
    assert 'data-model-id="test/model-b"' in response.text
    assert 'data-model-id="test/model-a"' not in response.text
    text_only = auth_client.get("/settings/models", params={"q": "text only"})
    assert "No models match." in text_only.text
    everything = auth_client.get("/settings/models", params={"q": "text only", "all": "1"})
    assert 'data-model-id="test/text-only"' in everything.text
    assert "text only</span>" in everything.text
    assert "captures will fail with this model" in everything.text
    best_effort = auth_client.get("/settings/models", params={"q": "nova"})
    assert "best effort" in best_effort.text
    json_only = auth_client.get("/settings/models", params={"q": "2.5-flash-image"})
    assert ">JSON<" in json_only.text


def test_estimate_uses_recent_successful_calls(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    now = datetime.now(UTC)
    with open_db(auth_settings) as conn:
        for tokens in (1000, 3000):
            record_ai_call(
                conn, auth_settings, None, now, Usage(input_tokens=tokens, output_tokens=200), "ok"
            )
        record_ai_call(conn, auth_settings, None, now, Usage(input_tokens=99_999), "timeout")
    html = auth_client.get("/settings/models", params={"q": "model-a"}).text
    assert "about 2,000 input +" in html and "200 output tokens (average" in html


# ---------------------------------------------------------------- choosing


def test_choose_model_persists_and_rerenders(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    response = _choose(auth_client, "test/model-b")
    assert response.status_code == 200
    assert response.text.lstrip().startswith('<section id="ai-model"')
    assert "<code>test/model-b</code>" in response.text
    assert "Chosen here (AI_MODEL in .env is <code>test/model-a</code>)" in response.text
    assert 'id="ai-provider" hx-swap-oob="true">fake / test/model-b' in response.text
    with open_db(auth_settings) as conn:
        assert stored_model(conn) == "test/model-b"
        assert effective_model(conn, auth_settings) == "test/model-b"
    page = auth_client.get("/settings").text
    assert "fake / test/model-b" in page and 'id="ai-model-reset"' in page


def test_reset_falls_back_to_env(auth_client: TestClient, auth_settings: Settings) -> None:
    _choose(auth_client, "test/model-b")
    response = auth_client.post("/settings/model/reset", headers=HX)
    assert response.status_code == 200
    assert "Using AI_MODEL from .env." in response.text
    assert "<code>test/model-a</code>" in response.text
    with open_db(auth_settings) as conn:
        assert stored_model(conn) is None
        assert effective_model(conn, auth_settings) == "test/model-a"


@pytest.mark.parametrize(
    "model",
    [
        "claude-sonnet-5-5",  # not an OpenRouter id
        "vendor/model name",
        "<b>x</b>/y",
        "vendor/not-in-catalog",
        "openrouter/auto",  # price unknown: not offered
        "test/price-unknown",
    ],
)
def test_invalid_or_unknown_model_is_rejected(
    auth_client: TestClient, auth_settings: Settings, model: str
) -> None:
    response = _choose(auth_client, model)
    assert response.status_code == 400
    with open_db(auth_settings) as conn:
        assert stored_model(conn) is None


def test_choose_needs_htmx_and_same_origin(auth_settings: Settings) -> None:
    with logged_in(auth_settings) as client:
        no_hx = client.post("/settings/model", data={"model": "test/model-b"})
        assert no_hx.status_code == 403
        assert client.post("/settings/model/reset").status_code == 403
        cross = client.post(
            "/settings/model",
            data={"model": "test/model-b"},
            headers=HX | {"Origin": "http://evil.example"},
        )
        assert cross.status_code == 403
    with open_db(auth_settings) as conn:
        assert stored_model(conn) is None


def test_non_image_model_is_allowed_with_a_warning(auth_client: TestClient) -> None:
    response = _choose(auth_client, "test/text-only")
    assert response.status_code == 200
    assert "Model test/text-only does not accept images" in response.text
    assert "photo, receipt and reconcile captures will fail" in response.text


def test_model_missing_from_fresh_catalog_warns(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    with open_db(auth_settings) as conn:
        model_setting.set_model(conn, "vendor/retired-model")
    html = auth_client.get("/settings").text
    assert "Model vendor/retired-model is no longer available on OpenRouter" in html


def test_catalog_unavailable_degrades(make_settings: SettingsFactory, tmp_path: Path) -> None:
    settings = make_settings(
        household_password=TEST_PASSWORD, openrouter_catalog_file=tmp_path / "missing.json"
    )
    with logged_in(settings) as client:
        html = client.get("/settings").text
        assert "model list is unavailable right now; the current model keeps working" in html
        # Without a catalog a well-formed id is accepted (it is validated when used).
        assert _choose(client, "vendor/some-model").status_code == 200
        assert _choose(client, "not an id").status_code == 400


# ---------------------------------------------------------------- visibility


@pytest.mark.parametrize(
    "overrides",
    [
        {"ai_provider": "anthropic", "ai_api_key": "sk-ant-test"},
        {"ai_provider": "fake"},  # no catalog file
    ],
)
def test_picker_hidden_when_not_selectable(make_settings: SettingsFactory, overrides: dict) -> None:
    settings = make_settings(household_password=TEST_PASSWORD, **overrides)
    with logged_in(settings) as client:
        html = client.get("/settings").text
        assert 'id="ai-model"' not in html
        assert client.get("/settings/models").status_code == 404
        assert _choose(client, "test/model-b").status_code == 404
        assert client.post("/settings/model/reset", headers=HX).status_code == 404


def test_anthropic_ignores_a_stored_model(make_settings: SettingsFactory) -> None:
    settings = make_settings(ai_provider="anthropic", ai_api_key="sk-x", ai_model="claude-x")
    with open_db(settings) as conn:
        migrate(conn)
        model_setting.set_model(conn, "test/model-b")
        assert effective_model(conn, settings) == "claude-x"


def test_openrouter_settings_page(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        household_password=TEST_PASSWORD,
        ai_provider="openrouter",
        openrouter_api_key="sk-or-v1-secret",
        ai_model="google/gemini-3.1-flash-lite",
        monthly_ai_budget=5,
    )
    with logged_in(settings) as client:
        html = client.get("/settings").text
        assert "openrouter / google/gemini-3.1-flash-lite" in html
        assert 'id="ai-model"' in html
        assert "AI prices are not set" not in html  # OpenRouter reports real costs
        assert "sk-or-v1-secret" not in html


def test_openrouter_with_anthropic_model_id_is_disabled(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        household_password=TEST_PASSWORD,
        ai_provider="openrouter",
        openrouter_api_key="sk-or-v1-secret",
    )
    with logged_in(settings) as client:
        html = client.get("/settings").text
        assert "is not an OpenRouter model id" in html
        refused = post_text(client, "two milks")
        assert "is not an OpenRouter model id" in refused.text
        # Picking a valid model on the page enables AI capture again.
        assert _choose(client, "test/model-b").status_code == 200
        assert "is not an OpenRouter model id" not in client.get("/settings").text


# ---------------------------------------------------------------- worker


def test_next_capture_uses_and_records_the_chosen_model(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    worker = Worker(auth_settings, provider_for=make_provider_for(auth_settings))

    first = capture_id(post_text(auth_client, "milk"))
    _tick(auth_settings, worker)
    _choose(auth_client, "test/model-b")
    second = capture_id(post_text(auth_client, "milk"))
    _tick(auth_settings, worker)
    auth_client.post("/settings/model/reset", headers=HX)
    third = capture_id(post_text(auth_client, "eggs"))
    _tick(auth_settings, worker)

    with open_db(auth_settings) as conn:
        rows = [capture_row(conn, cid) for cid in (first, second, third)]
        assert [(r["status"], r["provider"], r["model"]) for r in rows] == [
            ("done", "fake", "test/model-a"),
            ("done", "fake", "test/model-b"),
            ("done", "fake", "test/model-a"),
        ]
        # Model B answered from its own script; the default script continued where it was.
        names = [[i["name"] for i in capture_items(conn, cid)] for cid in (first, second, third)]
        assert names == [["Default milk"], ["Model B milk"], ["Default eggs"]]


def test_worker_with_fixed_provider_still_works(auth_settings: Settings) -> None:
    worker = Worker(auth_settings, None)
    assert worker.provider_for("anything") is None


def test_disabled_model_fails_capture_with_reason(make_settings: SettingsFactory) -> None:
    settings = make_settings(ai_provider="openrouter", openrouter_api_key="k")
    with open_db(settings) as conn:
        migrate(conn)
        cid = service.create_capture(
            conn, capture_type="text", now=datetime.now(UTC), input_text="milk"
        )
        worker = Worker(settings, provider_for=make_provider_for(settings))
        assert worker._tick(conn)
        row: sqlite3.Row = capture_row(conn, cid)
        assert row["status"] == "failed"
        assert "is not an OpenRouter model id" in row["error"]


class _NoCostOpenRouter:
    """Stands in for an OpenRouter provider whose response carried no ``usage.cost``."""

    name = "openrouter"
    model = "test/model-a"

    def extract(self, req: ExtractRequest) -> ExtractResult:
        return ExtractResult(items=[], usage=Usage(input_tokens=1_000, output_tokens=100))


def test_ledger_falls_back_to_catalog_prices(make_settings: SettingsFactory) -> None:
    settings = make_settings(ai_provider="openrouter", openrouter_api_key="k")
    with open_db(settings) as conn:
        migrate(conn)
        cid = service.create_capture(
            conn, capture_type="text", now=datetime.now(UTC), input_text="milk"
        )
        assert run_once(conn, settings, _NoCostOpenRouter())
        (call,) = conn.execute("SELECT * FROM ai_calls").fetchall()
        # test/model-a costs $0.50 / $2.00 per 1M tokens: 1000 * 0.5 + 100 * 2 micros.
        assert call["capture_id"] == cid and call["cost_micros"] == 700
        assert capture_row(conn, cid)["model"] == "test/model-a"


class _TimingOutOpenRouter:
    """An OpenRouter provider stand-in whose call ends without a usage report."""

    name = "openrouter"
    model = "test/model-a"
    max_tokens = 4096

    def __init__(self, error: Exception) -> None:
        self.error = error

    def extract(self, req: ExtractRequest) -> ExtractResult:
        raise self.error


@pytest.mark.parametrize(
    ("error", "charged"),
    [
        (ProviderTimeoutError(maybe_billed=True), True),  # read timeout: may be billed
        (ProviderTransientError("dropped", maybe_billed=True), True),
        (ProviderTimeoutError(), False),  # never reached OpenRouter (connect timeout)
    ],
)
def test_capture_timeouts_are_charged_an_estimate(
    make_settings: SettingsFactory, error: Exception, charged: bool
) -> None:
    settings = make_settings(ai_provider="openrouter", openrouter_api_key="k")
    with open_db(settings) as conn:
        migrate(conn)
        service.create_capture(conn, capture_type="text", now=datetime.now(UTC), input_text="x")
        assert run_once(conn, settings, _TimingOutOpenRouter(error))
        (call,) = conn.execute("SELECT * FROM ai_calls").fetchall()
        # No history yet: 1,500 input tokens + the 4,096 output cap at $0.50 / $2.00 per 1M.
        expected = round(1_500 * 0.5 + 4_096 * 2.0) if charged else 0
        assert call["cost_micros"] == expected

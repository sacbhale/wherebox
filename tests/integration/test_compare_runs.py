"""Comparison runs (spec C1, R3, R4, R13): the /compare page and its empty state, Settings'
case count, availability, model/case validation, the rough estimate and 2x budget refusal,
one run at a time (also under a concurrent double submit), status polling and cancel, and
the status UI (spec-compare-fixes A): below the Start button and scrolled into view, the form
locked while running, per-model progress with plain errors, the finished summary and the
out-of-band Past comparisons refresh."""

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.ai import openrouter_catalog
from app.ai.fake import FakeProvider
from app.ai.provider import ProviderPermanentError, ProviderRateLimited
from app.ai.schemas import Usage
from app.capture.budget import record_ai_call
from app.compare import execution, runs
from app.config import Settings
from app.db.connection import connect
from tests.compare_helpers import CATALOG, T0, done_capture, extract, make_case
from tests.conftest import TEST_PASSWORD, SettingsFactory, logged_in
from tests.inventory_helpers import HX, open_db

MODELS = ["test/model-a", "test/model-b"]


@pytest.fixture(autouse=True)
def _fresh_catalog() -> Iterator[None]:
    openrouter_catalog.clear_cache()
    yield
    openrouter_catalog.clear_cache()


@pytest.fixture
def auth_settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(
        household_password=TEST_PASSWORD, openrouter_catalog_file=CATALOG, ai_model="test/model-a"
    )


def _cases(settings: Settings, n: int = 2) -> list[int]:
    with open_db(settings) as conn:
        return [make_case(conn, settings, [f"Thing {i}", "Milk"]) for i in range(n)]


def _start(client: TestClient, models: list[str], cases: list[int] | None = None):
    data: dict[str, list[str]] = {"models": models}
    if cases is not None:
        data["cases"] = [str(c) for c in cases]
    return client.post("/compare/runs", data=data, headers=HX)


def _runs(settings: Settings) -> list:
    with open_db(settings) as conn:
        return conn.execute("SELECT * FROM comparison_runs ORDER BY id").fetchall()


# ---------------------------------------------------------------- pages


def test_empty_state_and_settings_link(auth_client: TestClient) -> None:
    html = auth_client.get("/compare").text
    assert 'id="compare-empty"' in html
    assert "Use for model comparison" in html and 'href="/captures"' in html
    assert "0 comparison cases" in html
    assert 'id="compare-form"' not in html  # nothing to compare yet
    settings_page = auth_client.get("/settings").text
    assert 'id="compare-cases-count">0 comparison cases' in settings_page
    assert 'href="/compare"' in settings_page


def test_page_with_cases(auth_client: TestClient, auth_settings: Settings) -> None:
    case_ids = _cases(auth_settings)
    html = auth_client.get("/compare").text
    assert "2 comparison cases" in html
    for case_id in case_ids:
        assert f'name="cases" value="{case_id}"\n        checked' in html
    assert 'data-model-id="test/model-b"' in html  # picker rows
    assert 'data-model-id="openrouter/free"' not in html  # routers excluded
    assert 'data-model-id="test/price-unknown"' not in html
    assert "Rough estimate" in html
    assert 'id="compare-cases-count">2 comparison cases' in auth_client.get("/settings").text


def test_unavailable_without_openrouter_key(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(household_password=TEST_PASSWORD)  # fake, no catalog file

    def no_catalog(*args, **kwargs):
        raise AssertionError("the catalog is not needed when comparisons are unavailable")

    monkeypatch.setattr(openrouter_catalog, "load_catalog", no_catalog)
    with logged_in(settings) as client:
        html = client.get("/compare").text
        assert "needs an OpenRouter key" in html
        assert 'id="compare-form"' not in html
        assert client.get("/compare/models").status_code == 404
        assert client.get("/compare/selection").status_code == 404
        assert _start(client, MODELS).status_code == 404
        assert "needs an OpenRouter key" in client.get("/settings").text
        # Finished captures do not offer "Use for model comparison" either.
        with open_db(settings) as conn:
            done_capture(
                conn, settings, "text", extract("Milk"), text="milk", now=datetime.now(UTC)
            )
        captures = client.get("/captures").text
        assert 'data-capture-status="done"' in captures
        assert "Use for model comparison" not in captures


def test_available_with_openrouter_key_under_anthropic(
    make_settings: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OPENROUTER_CATALOG_FILE only counts under the fake provider: stub the fetch instead.
    monkeypatch.setattr(openrouter_catalog, "fetch_models", lambda: json.loads(CATALOG.read_text()))
    settings = make_settings(
        household_password=TEST_PASSWORD,
        ai_provider="anthropic",
        ai_api_key="sk-ant-x",
        openrouter_api_key="sk-or-v1-secret",
        openrouter_catalog_file=CATALOG,  # ignored under anthropic
    )
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            make_case(conn, settings, ["Milk"])
        html = client.get("/compare").text
        assert 'id="compare-form"' in html and "sk-or-v1-secret" not in html
        assert _start(client, MODELS).status_code == 200


# ---------------------------------------------------------------- starting


def test_start_creates_run_and_pending_results(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    case_ids = _cases(auth_settings)
    response = _start(auth_client, MODELS, case_ids)
    assert response.status_code == 200
    assert 'data-run-status="running"' in response.text
    assert 'hx-trigger="every 2s"' in response.text
    (run,) = _runs(auth_settings)
    assert run["status"] == "running"
    assert json.loads(run["models_json"]) == MODELS
    ordered = json.loads(run["case_ids_json"])
    assert ordered == sorted(case_ids, reverse=True)  # newest capture first
    # Per call: the average of the recent ok calls (the cases' captures, 100 in / 10 out):
    # test/model-a 100*0.5+10*2 = 70 micros, model-b 100*1+10*4 = 140, for 2 cases.
    assert run["estimate_micros"] == 2 * (70 + 140)
    with open_db(auth_settings) as conn:
        results = runs.results(conn, run["id"])
    assert [(r["case_id"], r["model"], r["status"]) for r in results] == [
        (ordered[0], "test/model-a", "pending"),
        (ordered[0], "test/model-b", "pending"),
        (ordered[1], "test/model-a", "pending"),
        (ordered[1], "test/model-b", "pending"),
    ]


def test_default_cases_are_all_usable(auth_client: TestClient, auth_settings: Settings) -> None:
    case_ids = _cases(auth_settings, 3)
    assert _start(auth_client, MODELS).status_code == 200
    (run,) = _runs(auth_settings)
    assert sorted(json.loads(run["case_ids_json"])) == sorted(case_ids)


@pytest.mark.parametrize(
    ("models", "status", "message"),
    [
        (["test/model-a"], 409, "Pick 2 to 5 different models."),
        (["test/model-a", "test/model-a"], 409, "Pick 2 to 5 different models."),
        (
            ["test/model-a", "test/model-b", "openai/gpt-5-nano", "google/gemini-3.1-flash-lite",
             "anthropic/claude-sonnet-5.5", "amazon/nova-lite-v1"],
            409,
            "Pick 2 to 5 different models.",
        ),
        (["test/model-a", "vendor/not-in-catalog"], 400, "is not in the OpenRouter model list"),
        (["test/model-a", "openrouter/free"], 400, "cannot be compared"),
        (["test/model-a", "openrouter/auto"], 400, "cannot be compared"),
        (["test/model-a", "test/price-unknown"], 400, "has no published price"),
        (["test/model-a", "<b>x</b>"], 400, "is not an OpenRouter model id"),
    ],
)  # fmt: skip
def test_model_validation(
    auth_client: TestClient, auth_settings: Settings, models: list[str], status: int, message: str
) -> None:
    _cases(auth_settings, 1)
    response = _start(auth_client, models)
    assert response.status_code == status
    assert message in response.text
    assert _runs(auth_settings) == []


def test_five_models_are_allowed(auth_client: TestClient, auth_settings: Settings) -> None:
    _cases(auth_settings, 1)
    five = [*MODELS, "openai/gpt-5-nano", "google/gemini-3.1-flash-lite", "amazon/nova-lite-v1"]
    assert _start(auth_client, five).status_code == 200


def test_case_validation(auth_client: TestClient, auth_settings: Settings) -> None:
    no_cases = _start(auth_client, MODELS)
    assert no_cases.status_code == 409
    assert "no usable comparison cases" in no_cases.text
    case_ids = _cases(auth_settings, 1)
    assert _start(auth_client, MODELS, [case_ids[0] + 100]).status_code == 400
    assert _start(auth_client, MODELS, list(range(1000, 1026))).status_code == 409
    assert (
        auth_client.post(
            "/compare/runs", data={"models": MODELS, "cases": ["x"]}, headers=HX
        ).status_code
        == 400
    )
    assert _runs(auth_settings) == []


def test_catalog_unavailable_refuses(make_settings: SettingsFactory, tmp_path) -> None:
    settings = make_settings(
        household_password=TEST_PASSWORD, openrouter_catalog_file=tmp_path / "missing.json"
    )
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            make_case(conn, settings, ["Milk"])
        response = _start(client, MODELS)
        assert response.status_code == 409
        assert "model list is unavailable" in response.text


@pytest.mark.parametrize(("budget", "status"), [(0.00141, 409), (0.00142, 200)])
def test_budget_refuses_at_twice_the_estimate(
    make_settings: SettingsFactory, budget: float, status: int
) -> None:
    settings = make_settings(
        household_password=TEST_PASSWORD,
        openrouter_catalog_file=CATALOG,
        monthly_ai_budget=budget,
        ai_input_price_per_mtok=0,
        ai_output_price_per_mtok=0,
    )
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            make_case(conn, settings, ["Milk"])  # its capture: 100 in / 10 out tokens
            spent = Usage(input_tokens=1, cost_usd=0.001)  # 1,000 micros spent
            record_ai_call(conn, settings, None, datetime.now(UTC), spent, "permanent_error")
        # Rough estimate for one case: model-a 100*0.5+10*2 = 70, model-b 100*1+10*4 = 140
        # micros, so 210; refused when 1,000 + 2 x 210 exceeds the budget.
        html = client.get("/compare").text
        assert 'id="compare-budget"' in html
        response = _start(client, MODELS)
        assert response.status_code == status
        if status == 409:
            assert "twice the rough estimate" in response.text
            assert _runs(settings) == []
        else:
            assert _runs(settings)[0]["estimate_micros"] == 210


def test_one_run_at_a_time(auth_client: TestClient, auth_settings: Settings) -> None:
    _cases(auth_settings, 1)
    assert _start(auth_client, MODELS).status_code == 200
    second = _start(auth_client, MODELS)
    assert second.status_code == 409
    assert "already running" in second.text
    assert len(_runs(auth_settings)) == 1
    html = auth_client.get("/compare").text
    assert 'data-run-status="running"' in html
    # The form is shown locked while the run is running.
    assert '<fieldset id="compare-controls" class="compare-controls" disabled>' in html
    assert "A comparison is running…" in html


def test_concurrent_double_submit_creates_one_run(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    case_ids = _cases(auth_settings, 1)
    barrier = threading.Barrier(4)
    outcomes: list[str] = []

    def submit() -> None:
        conn = connect(auth_settings.db_path)
        try:
            barrier.wait()
            runs.start(conn, MODELS, case_ids, 0, T0)
            outcomes.append("started")
        except runs.RunConflict:
            outcomes.append("conflict")
        finally:
            conn.close()

    threads = [threading.Thread(target=submit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict"] * 3 + ["started"]
    assert len(_runs(auth_settings)) == 1


# ---------------------------------------------------------------- status and cancel


def test_status_polls_until_finished_and_cancel(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    _cases(auth_settings, 1)
    _start(auth_client, MODELS)
    (run,) = _runs(auth_settings)
    status = auth_client.get(f"/compare/runs/{run['id']}/status")
    assert status.status_code == 200 and "0 of 2 calls done" in status.text

    cancelled = auth_client.post(f"/compare/runs/{run['id']}/cancel", headers=HX)
    assert cancelled.status_code == 286
    assert 'data-run-status="cancelled"' in cancelled.text
    assert "hx-trigger" not in cancelled.text
    with open_db(auth_settings) as conn:
        assert [r["status"] for r in runs.results(conn, run["id"])] == ["cancelled"] * 2
        assert runs.get(conn, run["id"])["finished_at"] is not None
    assert auth_client.get(f"/compare/runs/{run['id']}/status").status_code == 286
    again = auth_client.post(f"/compare/runs/{run['id']}/cancel", headers=HX)
    assert again.status_code == 409
    assert auth_client.post("/compare/runs/999/cancel", headers=HX).status_code == 404
    assert auth_client.get("/compare/runs/999/status").status_code == 404
    # A new run can start once the previous one is cancelled.
    assert _start(auth_client, MODELS).status_code == 200


def test_mutations_need_htmx(auth_client: TestClient, auth_settings: Settings) -> None:
    _cases(auth_settings, 1)
    assert auth_client.post("/compare/runs", data={"models": MODELS}).status_code == 403
    _start(auth_client, MODELS)
    (run,) = _runs(auth_settings)
    assert auth_client.post(f"/compare/runs/{run['id']}/cancel").status_code == 403
    assert (
        auth_client.post(
            f"/compare/runs/{run['id']}/use", data={"model": "test/model-b"}
        ).status_code
        == 403
    )


# ---------------------------------------------------------------- status UI (spec-compare-fixes A)

THREE = ["test/model-a", "test/model-b", "openai/gpt-5-nano"]
LOCKED_BUTTON = (
    '<button type="submit" id="compare-start-button" disabled data-locked hx-swap-oob="true">'
    "A comparison is running…</button>"
)
UNLOCKED_BUTTON = (
    '<button type="submit" id="compare-start-button" hx-swap-oob="true">Start comparison</button>'
)


def _run_all(settings: Settings, scripts: dict, now: datetime | None = None) -> None:
    """Execute every claimable result with per-model fake scripts (at ``now``)."""
    fake = FakeProvider(models=scripts)
    with open_db(settings) as conn:
        while execution.run_once(conn, settings, fake.with_model, now=now):
            pass


def test_status_is_below_the_start_button_and_scrolled_into_view(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    _cases(auth_settings, 1)
    page = auth_client.get("/compare").text
    form = page[page.index('<form id="compare-form"') :]
    assert 'hx-target="#compare-run"' in form[: form.index(">")]
    # Scrolled into view: the status box itself (htmx would otherwise scroll to the last
    # swapped element, the out-of-band Past comparisons list below it).
    assert 'hx-swap="innerHTML show:#compare-run:bottom"' in form[: form.index(">")]
    # An absolute selector: the case picker and Remove buttons inherit it.
    assert 'hx-disabled-elt="#compare-start-button"' in form[: form.index(">")]
    # The status box comes right after the Start button and the form, outside the
    # (lockable) controls, so its Cancel button stays usable.
    button = page.index('id="compare-start-button"')
    status_box = page.index('<div id="compare-run"')
    assert button < page.index("</form>", button) < page.index("</fieldset>", button) < status_box
    assert status_box < page.index("Past comparisons")

    started = _start(auth_client, THREE)
    assert started.status_code == 200
    (run,) = _runs(auth_settings)
    assert started.text.lstrip().startswith(
        f'<div class="compare-status status-running" id="run-{run["id"]}"'
    )
    # Out of band: the start button locked, the Past comparisons list with the new run.
    assert LOCKED_BUTTON in started.text
    recent = started.text[started.text.index('<div id="compare-recent" hx-swap-oob="true">') :]
    assert f'data-run-id="{run["id"]}"' in recent and "running" in recent

    # Reloaded while running: the status is in the same place, the form locked.
    page = auth_client.get("/compare").text
    button = page.index('id="compare-start-button"')
    assert button < page.index(f'id="run-{run["id"]}"')
    assert '<fieldset id="compare-controls" class="compare-controls" disabled>' in page
    assert "hx-swap-oob" not in page  # out-of-band parts only in htmx answers


def test_form_is_locked_while_running_and_unlocked_by_the_terminal_partial(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    _cases(auth_settings, 1)
    page = auth_client.get("/compare").text
    assert '<fieldset id="compare-controls" class="compare-controls">' in page
    assert ">Start comparison</button>" in page and "data-locked" not in page

    _start(auth_client, MODELS)
    (run,) = _runs(auth_settings)
    poll = auth_client.get(f"/compare/runs/{run['id']}/status")
    assert poll.status_code == 200 and "hx-swap-oob" not in poll.text  # nothing to change

    cancelled = auth_client.post(f"/compare/runs/{run['id']}/cancel?from=compare", headers=HX)
    assert cancelled.status_code == 286
    assert UNLOCKED_BUTTON in cancelled.text
    assert '<div id="compare-recent" hx-swap-oob="true">' in cancelled.text
    page = auth_client.get("/compare").text
    assert '<fieldset id="compare-controls" class="compare-controls">' in page


def test_status_lists_per_model_progress_with_plain_errors(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    _cases(auth_settings, 1)
    _start(auth_client, THREE)
    (run,) = _runs(auth_settings)
    raw = "OpenRouter HTTP 429: Provider returned error"
    moment = datetime.now(UTC)
    _run_all(
        auth_settings,
        {
            "test/model-a": [extract("Thing 0", "Milk")],
            "test/model-b": [ProviderRateLimited(raw)],
            "openai/gpt-5-nano": [ProviderPermanentError("OpenRouter HTTP 400: bad image")],
        },
        now=moment,
    )
    auth_client.app.state.clock = lambda: moment
    html = auth_client.get(f"/compare/runs/{run['id']}/status").text
    assert "Running: 2 of 3 calls done (1 failed)." in html
    rows = html[html.index('id="compare-model-progress"') : html.index('class="compare-actions"')]

    def row(model: str) -> str:
        start = rows.index(f'data-model-id="{model}"')
        return rows[start : rows.index("</li>", start)]

    assert "1/1 calls · 1 ok" in row("test/model-a") and "error" not in row("test/model-a")
    limited = row("test/model-b")
    assert "0/1 calls · 0 ok · 1 waiting to retry" in limited
    assert "Rate-limited by the model&#39;s provider — busy right now; waiting to retry." in limited
    assert "Trying again\n        in about 30 s." in limited
    # The provider's own message, reachable on a phone (not only a title= tooltip).
    assert (
        f'<details class="model-progress-detail"><summary>Details</summary><code>{raw}</code>'
        in limited
    )
    assert "title=" not in limited
    # The retry time is computed on every poll from not_before, not frozen at failure time.
    auth_client.app.state.clock = lambda: moment + timedelta(seconds=21)
    later = auth_client.get(f"/compare/runs/{run['id']}/status").text
    assert "in about 9 s." in later and "in about 30 s." not in later
    auth_client.app.state.clock = lambda: moment + timedelta(seconds=45)
    assert "Trying again\n        now." in auth_client.get(f"/compare/runs/{run['id']}/status").text
    failed = row("openai/gpt-5-nano")
    assert "1/1 calls · 0 ok · 1 failed" in failed
    assert 'class="model-progress-error error"' in failed and "bad image" in failed
    assert "waiting" not in failed and "<details" not in failed
    assert "sk-" not in html


def test_out_of_band_parts_only_for_the_compare_start_form(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    """The report page (and /compare without a start form) have no start button: their
    polls and Cancel never get out-of-band parts (htmx:oobErrorNoTarget)."""
    case_ids = _cases(auth_settings, 1)
    _start(auth_client, MODELS)
    (run,) = _runs(auth_settings)
    status_url = f"/compare/runs/{run['id']}/status"
    page = auth_client.get("/compare").text
    assert f'hx-get="{status_url}?from=compare"' in page
    assert f'hx-post="/compare/runs/{run["id"]}/cancel?from=compare"' in page
    report_page = auth_client.get(f"/compare/runs/{run['id']}").text
    assert f'hx-get="{status_url}"' in report_page and "from=compare" not in report_page
    # A poll answer from /compare keeps polling with the parameter.
    assert (
        f'hx-get="{status_url}?from=compare"' in auth_client.get(f"{status_url}?from=compare").text
    )

    # /compare whose cases are all unusable has no start form: no parameter either.
    with open_db(auth_settings) as conn:
        conn.execute(
            "UPDATE captures SET input_text = NULL"
            " WHERE id = (SELECT capture_id FROM benchmark_cases WHERE id = ?)",
            (case_ids[0],),
        )
        conn.commit()
    bare = auth_client.get("/compare").text
    assert 'id="compare-start-button"' not in bare
    assert f'hx-get="{status_url}"' in bare and "from=compare" not in bare

    cancelled = auth_client.post(f"/compare/runs/{run['id']}/cancel", headers=HX)
    assert cancelled.status_code == 286 and "hx-swap-oob" not in cancelled.text
    plain = auth_client.get(status_url)
    assert plain.status_code == 286 and "hx-swap-oob" not in plain.text
    from_compare = auth_client.get(f"{status_url}?from=compare")
    assert from_compare.status_code == 286
    assert UNLOCKED_BUTTON in from_compare.text
    assert '<div id="compare-recent" hx-swap-oob="true">' in from_compare.text


def test_finished_partial_shows_the_summary_report_link_and_past_runs(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    _cases(auth_settings, 1)
    _start(auth_client, MODELS)
    (run,) = _runs(auth_settings)
    _run_all(
        auth_settings,
        {"test/model-a": [extract("Thing 0")], "test/model-b": [extract("Thing 0", "Milk")]},
    )
    done = auth_client.get(f"/compare/runs/{run['id']}/status?from=compare")
    assert done.status_code == 286
    assert 'data-run-status="done"' in done.text and "hx-trigger" not in done.text
    assert "Finished: 2 of 2 calls succeeded" in done.text
    assert 'id="compare-top" data-model-id="test/model-b"' in done.text
    assert "F1 1.00" in done.text
    assert f'href="/compare/runs/{run["id"]}">See the report</a>' in done.text
    assert UNLOCKED_BUTTON in done.text
    recent = done.text[done.text.index('<div id="compare-recent" hx-swap-oob="true">') :]
    first = recent.index("<li")
    assert recent[first:].startswith(f'<li data-run-id="{run["id"]}">')
    assert "done ·" in recent
    assert "2/2 calls" in recent
    # The report shows the finished status with the same summary.
    report_page = auth_client.get(f"/compare/runs/{run['id']}").text
    assert 'id="compare-top"' in report_page and "hx-swap-oob" not in report_page


def test_picker_notes_that_free_variants_are_often_rate_limited(
    auth_client: TestClient,
) -> None:
    html = auth_client.get("/compare/models", params={"q": "free", "all": "1"}).text
    rows = html.split('<li class="model-row"')[1:]
    free = [row for row in rows if ':free"' in row]
    paid = [row for row in rows if ':free"' not in row]
    assert free, "the catalog fixture has free variants"
    assert all("often rate-limited" in row for row in free)
    assert not any("often rate-limited" in row for row in paid)
    # Settings' picker (the same row macro) has no such note.
    settings_rows = auth_client.get("/settings/models?q=free&all=1")
    assert settings_rows.status_code == 200 and ':free"' in settings_rows.text
    assert "often rate-limited" not in settings_rows.text

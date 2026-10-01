"""Model comparison on a phone (Chromium + WebKit, 390x844): make a case from a finished text
capture, compare two models with per-model fake scripts, watch the run finish, see the
better model ranked first, and make it the active model with "Use this model". A 3-model run
(with many past runs below) shows its status right below Start, fully in view, without htmx
console errors while models and cases change; it locks the form, reports per-model progress with
plain errors (a 429 retried until it succeeds, a permanent failure) and, once finished, the
summary, the report link and the new run under Past comparisons, all without a reload."""

import contextlib
import sqlite3
import time

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_mobile_flows import assert_touch_targets

NO_PAGE_SCROLL = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
GOOD = "test/model-b"
WEAK = "openai/gpt-5-nano"
BUSY = "amazon/nova-lite-v1"


def _step(*items: tuple[str, int]) -> dict:
    return {
        "result": {
            "items": [
                {"name": name, "quantity": qty, "kind": "consumable", "confidence": 0.9}
                for name, qty in items
            ],
            "usage": {"input_tokens": 300, "output_tokens": 90},
        }
    }


@pytest.mark.fake_steps(2)  # the text capture: Milk x2 and a cordless drill
@pytest.mark.fake_models(
    {
        GOOD: {"steps": [_step(("Milk", 2), ("Cordless drill", 1), ("Duct tape", 1))]},
        WEAK: {"steps": [_step(("Silk", 2), ("Hammer", 1))]},
    }
)
def test_compare_two_models(server, page: Page) -> None:
    response = server.http.post(
        "/captures/text",
        data={"text": "two milks, a drill and duct tape"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code in (200, 286), response.text
    server.wait_for_captures()

    # A case from the capture: the form is pre-filled; add the item the model missed.
    server.login(page, "/captures")
    page.get_by_role("link", name="Use for model comparison").tap()
    rows = page.locator("#case-rows .case-row")
    expect(rows.nth(0).locator('input[name="name"]')).to_have_value("Milk")
    expect(rows.nth(0).locator('input[name="quantity"]')).to_have_value("2")
    expect(rows.nth(1).locator('input[name="name"]')).to_have_value("Cordless drill")
    assert page.evaluate(NO_PAGE_SCROLL)
    assert_touch_targets(page)
    page.locator("#case-add-row").tap()
    expect(rows).to_have_count(4)
    rows.nth(3).locator('input[name="name"]').fill("Duct tape")
    page.locator("#case-save").tap()
    page.wait_for_url(server.url("/compare"))
    expect(page.locator("#case-count")).to_have_text("1 comparison case")
    assert page.evaluate(NO_PAGE_SCROLL)
    assert_touch_targets(page)

    # Pick two models and start.
    for query, model in (("model-b", GOOD), ("gpt-5-nano", WEAK)):
        page.fill("#compare-query", query)
        row = page.locator(f'#compare-models [data-model-id="{model}"]')
        expect(row).to_be_visible()
        row.get_by_role("button", name="Add").tap()
        expect(page.locator(f'#compare-selection [data-model-id="{model}"]')).to_be_visible()
    expect(page.locator("#compare-estimate")).to_contain_text("1 case \u00d7 2 models")
    page.locator("#compare-start-button").tap()
    expect(page.locator('[data-run-status="done"]')).to_be_visible(timeout=20_000)

    # The report ranks the better model first.
    page.locator("#compare-report-link").tap()
    summary_rows = page.locator("#compare-summary tbody tr")
    expect(summary_rows).to_have_count(2)
    expect(summary_rows.nth(0)).to_have_attribute("data-model-id", GOOD)
    expect(summary_rows.nth(1)).to_have_attribute("data-model-id", WEAK)
    expect(summary_rows.nth(0)).to_contain_text("best value (indicative)")
    assert page.evaluate(NO_PAGE_SCROLL)
    assert_touch_targets(page)
    page.locator(".case-detail summary").first.tap()
    expect(page.locator(".case-detail .line-missed").first).to_be_visible()

    summary_rows.nth(0).get_by_role("button", name="Use this model").tap()
    expect(summary_rows.nth(0).locator(".active-model")).to_have_text("Active")
    page.goto(server.url("/settings"))
    expect(page.locator("#ai-model-current")).to_contain_text(GOOD)
    expect(page.locator("#compare-cases-count")).to_have_text("1 comparison case")
    assert len(server.query("SELECT id FROM ai_calls WHERE comparison_result_id IS NOT NULL")) == 2


@pytest.mark.fake_steps(1)
def test_start_errors_are_shown_but_not_those_of_inner_requests(server, page: Page) -> None:
    """data-show-errors swaps the form's own 4xx HTML answer (a refused start), never a 4xx
    answer to a request of an element inside the form (the case picker)."""
    response = server.http.post(
        "/captures/text", data={"text": "milk"}, headers={"HX-Request": "true"}
    )
    assert response.status_code in (200, 286), response.text
    server.wait_for_captures()
    (capture,) = server.query("SELECT id FROM captures")
    response = server.http.post(
        "/compare/cases", data={"capture_id": str(capture["id"]), "name": "Milk", "quantity": "1"}
    )
    assert response.status_code == 303, response.text

    server.login(page, "/compare")
    # The case picker's own request fails (400, JSON): the selection stays as it was.
    box = page.locator('input[name="cases"]').first
    change = (
        "el => { el.value = 'not-a-number';"
        " el.dispatchEvent(new Event('change', {bubbles: true})); }"
    )
    with page.expect_response(lambda r: "/compare/selection" in r.url) as answer:
        box.evaluate(change)
    assert answer.value.status == 400
    page.wait_for_timeout(200)  # let htmx finish handling the answer
    expect(page.locator("#compare-selection")).to_be_visible()
    expect(page.locator("#compare-estimate")).to_be_visible()
    # Starting with no models is refused (HTML message): the message is shown.
    box.evaluate("el => { el.value = '1'; }")
    page.locator("#compare-start-button").tap()
    expect(page.locator("#compare-run")).to_contain_text("Pick 2 to 5 different models")


def _make_case(server) -> None:
    """A text capture (fake step 1: Milk) and a case expecting Milk, over HTTP."""
    response = server.http.post(
        "/captures/text", data={"text": "milk"}, headers={"HX-Request": "true"}
    )
    assert response.status_code in (200, 286), response.text
    server.wait_for_captures()
    (capture,) = server.query("SELECT id FROM captures")
    response = server.http.post(
        "/compare/cases", data={"capture_id": str(capture["id"]), "name": "Milk", "quantity": "1"}
    )
    assert response.status_code == 303, response.text


def _seed_past_runs(server, count: int) -> None:
    """``count`` finished runs, so Past comparisons reaches far below the status box."""
    with contextlib.closing(sqlite3.connect(server.data_dir / "inventory.db", timeout=10)) as db:
        db.executemany(
            "INSERT INTO comparison_runs (status, models_json, case_ids_json, estimate_micros,"
            " created_at, finished_at) VALUES ('done', '[]', '[]', 0, ?, ?)",
            [("2026-01-01T00:00:00.000Z", "2026-01-01T00:01:00.000Z")] * count,
        )
        db.commit()


def _skip_waits(server) -> None:
    """Fast-forward the comparison retry pauses (30 s, 60 s, ...): make every scheduled
    retry due now."""
    with contextlib.closing(sqlite3.connect(server.data_dir / "inventory.db", timeout=10)) as db:
        db.execute(
            "UPDATE comparison_results SET not_before = '2000-01-01T00:00:00.000Z'"
            " WHERE status = 'pending' AND attempts > 0"
        )
        db.commit()


@pytest.mark.fake_steps(1)
@pytest.mark.fake_models(
    {
        GOOD: {"steps": [_step(("Milk", 1))]},
        BUSY: {
            "steps": [
                {"error": "rate_limited", "retry_after_s": 1},
                {"error": "rate_limited"},
                _step(("Milk", 1)),
            ]
        },
        WEAK: {"steps": [{"error": "permanent", "message": "OpenRouter HTTP 400: bad request"}]},
    }
)
def test_three_model_run_shows_progress_and_finishes_without_reload(server, page: Page) -> None:
    _make_case(server)
    _seed_past_runs(server, 12)
    errors: list[str] = []
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    server.login(page, "/compare")
    expect(page.locator("#run-list li")).to_have_count(12)

    def add(query: str, model: str) -> None:
        page.fill("#compare-query", query)
        row = page.locator(f'#compare-models [data-model-id="{model}"]')
        expect(row).to_be_visible()
        row.get_by_role("button", name="Add").tap()
        expect(page.locator(f'#compare-selection [data-model-id="{model}"]')).to_be_visible()

    for query, model in (("model-b", GOOD), ("nova-lite", BUSY), ("gpt-5-nano", WEAK)):
        add(query, model)
    # Removing a model and changing the cases (requests of elements inside the form, which
    # inherit its hx-disabled-elt) log no htmx errors.
    chosen = page.locator(f'#compare-selection [data-model-id="{WEAK}"]')
    with page.expect_response(lambda r: "/compare/selection" in r.url):
        chosen.get_by_role("button", name="Remove").tap()
    expect(chosen).to_have_count(0)
    case_box = page.locator('input[name="cases"]').first
    for _toggle in range(2):
        with page.expect_response(lambda r: "/compare/selection" in r.url):
            case_box.tap()
    expect(case_box).to_be_checked()
    add("gpt-5-nano", WEAK)
    expect(page.locator("#compare-estimate")).to_contain_text("1 case \u00d7 3 models")
    page.wait_for_timeout(200)  # let htmx finish handling the last answer
    assert errors == [], errors
    page.evaluate("window.__sameDocument = true")  # gone after any reload

    start = page.locator("#compare-start-button")
    start.tap()
    status = page.locator("#compare-run .compare-status")
    expect(status).to_be_visible()
    # Right below Start and scrolled fully into view, although the out-of-band Past
    # comparisons list (the last element htmx swaps) reaches far below it.
    expect(status).to_be_in_viewport(ratio=1)
    page.wait_for_timeout(300)  # nothing scrolls it away afterwards
    expect(status).to_be_in_viewport(ratio=1)
    expect(status).to_have_attribute("data-run-status", "running")
    expect(start).to_be_disabled()
    expect(start).to_have_text("A comparison is running…")
    expect(page.locator("#compare-query")).to_be_disabled()  # the pickers are locked too
    expect(page.locator('input[name="cases"]').first).to_be_disabled()
    (run,) = server.query("SELECT id FROM comparison_runs WHERE status = 'running'")
    # Past comparisons already lists the new run.
    expect(page.locator("#run-list li").first).to_have_attribute("data-run-id", str(run["id"]))

    # Per-model progress, refreshed by the 2 s poll.
    rows = page.locator("#compare-model-progress")
    good, busy, weak = (rows.locator(f'[data-model-id="{m}"]') for m in (GOOD, BUSY, WEAK))
    expect(good).to_contain_text("1/1 calls · 1 ok", timeout=15_000)
    expect(weak).to_contain_text("1/1 calls · 0 ok · 1 failed", timeout=15_000)
    expect(weak).to_contain_text("OpenRouter HTTP 400: bad request")
    expect(busy).to_contain_text("Rate-limited by the model's provider", timeout=15_000)
    expect(busy).to_contain_text("0/1 calls · 0 ok · 1 waiting to retry")
    expect(busy).to_contain_text("Trying again in about")
    expect(page.locator("#compare-overall")).to_contain_text("2 of 3 calls done")
    assert page.evaluate(NO_PAGE_SCROLL)

    # An opened "Details" stays open across the 2 s polls that replace the status.
    busy_details = busy.locator("details.model-progress-detail")
    busy_details.locator("summary").tap()
    expect(busy_details).to_have_attribute("open", "")
    page.wait_for_timeout(4_500)
    expect(busy_details).to_have_attribute("open", "")

    # The 429 is retried (the pauses are fast-forwarded) until the third attempt succeeds.
    deadline = time.monotonic() + 30
    while not page.locator('[data-run-status="done"]').count():
        assert time.monotonic() < deadline, "the comparison did not finish"
        _skip_waits(server)
        page.wait_for_timeout(300)
    expect(busy).to_contain_text("1/1 calls · 1 ok")
    expect(busy).not_to_contain_text("Rate-limited")
    (result,) = server.query("SELECT attempts FROM comparison_results WHERE model = ?", BUSY)
    assert result["attempts"] == 3

    # Finished: summary, report link, Past runs refreshed and the form unlocked, no reload.
    expect(page.locator("#compare-overall")).to_contain_text("Finished: 2 of 3 calls succeeded")
    expect(page.locator("#compare-top")).to_contain_text("F1 1.00")
    report_link = page.get_by_role("link", name="See the report")
    expect(report_link).to_be_visible()
    first_run = page.locator("#run-list li").first
    expect(page.locator("#run-list li")).to_have_count(13)
    expect(first_run).to_have_attribute("data-run-id", str(run["id"]))
    expect(first_run).to_contain_text("done")
    expect(first_run).to_contain_text("3/3 calls")
    expect(start).to_be_enabled()
    expect(start).to_have_text("Start comparison")
    expect(page.locator("#compare-query")).to_be_enabled()
    assert page.evaluate("window.__sameDocument === true")
    assert page.evaluate(NO_PAGE_SCROLL)
    assert_touch_targets(page)
    assert errors == [], errors
    report_link.tap()
    page.wait_for_url(server.url(f"/compare/runs/{run['id']}"))

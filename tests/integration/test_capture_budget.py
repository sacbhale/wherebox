"""Monthly budget gating and the usage ledger (AC-23, AC-24), plus AI-disabled gating."""

import re
from datetime import UTC, datetime

import pytest

from app.ai.fake import FakeProvider
from app.ai.provider import ProviderInvalidOutput, ProviderPermanentError
from app.ai.schemas import Usage
from app.capture.budget import month_usage, record_ai_call
from app.inventory import locations
from tests.capture_helpers import (
    ai_calls,
    capture_id,
    capture_row,
    item,
    post_photo,
    post_receipt,
    post_text,
    result,
    run,
)
from tests.conftest import TEST_PASSWORD, logged_in
from tests.inventory_helpers import open_db

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
# $1 budget; $3 / $15 per million tokens: 100k input tokens cost $0.30.
BUDGET = {
    "monthly_ai_budget": 1.0,
    "ai_input_price_per_mtok": 3.0,
    "ai_output_price_per_mtok": 15.0,
}


@pytest.fixture
def budget_settings(make_settings):
    return make_settings(household_password=TEST_PASSWORD, **BUDGET)


@pytest.fixture
def client(budget_settings):
    with logged_in(budget_settings) as test_client:
        test_client.app.state.clock = lambda: NOW
        yield test_client


@pytest.fixture
def garage(client, budget_settings) -> int:
    with open_db(budget_settings) as conn:
        location_id, _ = locations.create(conn, "Garage")
    return location_id


def spend(settings, input_tokens: int, outcome: str = "ok") -> None:
    with open_db(settings) as conn:
        record_ai_call(conn, settings, None, NOW, Usage(input_tokens=input_tokens), outcome)


def test_exhausted_budget_refuses_captures_but_manual_add_works(client, budget_settings, garage):
    spend(budget_settings, 400_000)  # $1.20 >= $1.00
    for response in (
        post_photo(client, garage),
        post_receipt(client),
        post_text(client, "milk", garage),
    ):
        assert response.status_code == 409
        assert "Monthly AI budget reached" in response.text
    with open_db(budget_settings) as conn:
        assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0

    for panel in (
        client.get("/captures/panel", params={"loc": garage}),
        client.get("/captures/panel"),
    ):
        assert "Monthly AI budget reached" in panel.text
        submits = re.findall(r'<button type="submit"[^>]*>', panel.text)
        assert submits and all("disabled" in button for button in submits)
        assert re.search(r'<input type="file" name="photo"[^>]*disabled', panel.text)

    assert client.get(f"/items/new?loc={garage}").status_code == 200
    added = client.post(
        "/items/new",
        data={"name": "Rake", "quantity": "1", "kind": "belonging", "location_id": str(garage)},
    )
    assert added.status_code == 200 and "Rake" in added.text


def test_below_budget_capture_buttons_are_enabled(client, budget_settings, garage):
    spend(budget_settings, 100_000)  # $0.30
    panel = client.get("/captures/panel", params={"loc": garage}).text
    assert "budget" not in panel and "disabled" not in panel
    assert post_photo(client, garage).status_code == 200


def test_worker_rechecks_the_budget_before_calling(client, budget_settings, garage):
    cid = capture_id(post_photo(client, garage))
    spend(budget_settings, 400_000)  # the budget runs out while the capture waits
    provider = FakeProvider([result([item("Rake")])])
    run(budget_settings, provider, now=NOW)
    with open_db(budget_settings) as conn:
        capture = capture_row(conn, cid)
        assert ai_calls(conn, cid) == []
    assert (capture["status"], capture["error"]) == ("failed", "monthly budget reached")
    assert provider.requests == []
    status = client.get(f"/captures/{cid}/status")
    assert status.status_code == 286 and "Retry" in status.text


def test_ledger_counts_billed_invalid_outputs(client, budget_settings, garage):
    cid = capture_id(post_photo(client, garage))
    billed = ProviderInvalidOutput("bad tool input", Usage(input_tokens=340_000), raw="{}")
    run(budget_settings, FakeProvider([billed]), now=NOW)
    with open_db(budget_settings) as conn:
        capture = capture_row(conn, cid)
        calls = ai_calls(conn, cid)
        usage = month_usage(conn, budget_settings, NOW)
    assert capture["status"] == "failed" and capture["error"] == "bad tool input"
    assert [(c["outcome"], c["input_tokens"], c["cost_micros"]) for c in calls] == [
        ("invalid_output", 340_000, 1_020_000)
    ]
    assert usage.cost_micros == 1_020_000
    # That billed failure alone used up the budget.
    assert post_photo(client, garage).status_code == 409


def test_settings_total_equals_the_ledger(client, budget_settings, garage):
    provider = FakeProvider(
        [
            result([item("Rake")], input_tokens=12_345, output_tokens=678),
            ProviderInvalidOutput("bad", Usage(input_tokens=2_000, output_tokens=300), raw=""),
            ProviderPermanentError("HTTP 400", usage=Usage(input_tokens=50)),
        ]
    )
    for _ in range(3):
        capture_id(post_text(client, "a rake", garage))
        run(budget_settings, provider, now=NOW)
    with open_db(budget_settings) as conn:
        calls = ai_calls(conn)
        total = conn.execute("SELECT SUM(cost_micros) FROM ai_calls").fetchone()[0]
    assert [c["outcome"] for c in calls] == ["ok", "invalid_output", "permanent_error"]
    expected = (12_345 + 2_000 + 50) * 3 + (678 + 300) * 15
    assert total == expected
    page = client.get("/settings").text
    assert f"Cost: ${total / 1_000_000:.2f} USD" in page
    assert f"{12_345 + 2_000 + 50:,} input tokens" in page


def test_ai_disabled_refuses_captures_with_a_clear_message(make_settings):
    settings = make_settings(household_password=TEST_PASSWORD, ai_provider="anthropic")
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            garage, _ = locations.create(conn, "Garage")
        response = post_photo(client, garage)
        assert response.status_code == 409
        assert "AI capture is disabled" in response.text
        panel = client.get("/captures/panel").text
        assert "AI capture is disabled" in panel and "Add an item manually" in panel
        assert client.get("/items/new").status_code == 200

"""Settings model picker on a phone (Chromium + WebKit): search, pick "test/model-b", see it
active, and the next capture uses and records it. The e2e server runs the fake provider with
the test-only OPENROUTER_CATALOG_FILE, which shows the picker."""

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_mobile_flows import assert_touch_targets

NO_PAGE_SCROLL = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"


@pytest.mark.fake_steps(2)  # the text-capture step of tests/fixtures/fake_script.json
def test_pick_model_on_settings(server, page: Page) -> None:
    server.login(page, "/settings")
    expect(page.locator("#ai-model-current")).to_contain_text("test/model-a")
    expect(page.locator("#ai-model-source")).to_contain_text("Using AI_MODEL from .env")
    assert page.evaluate(NO_PAGE_SCROLL)
    assert_touch_targets(page)

    page.fill("#model-query", "model-b")
    row = page.locator('#model-results [data-model-id="test/model-b"]')
    expect(row).to_be_visible()
    expect(page.locator('#model-results [data-model-id="test/model-a"]')).to_have_count(0)
    expect(row).to_contain_text("per capture (rough)")
    row.get_by_role("button", name="Use").tap()

    expect(page.locator("#ai-model-current")).to_contain_text("test/model-b")
    expect(page.locator("#ai-model-source")).to_contain_text("Chosen here")
    expect(page.locator("#ai-provider")).to_contain_text("fake / test/model-b")
    assert page.evaluate(NO_PAGE_SCROLL)
    page.reload()
    expect(page.locator("#ai-model-current")).to_contain_text("test/model-b")
    assert server.query("SELECT value FROM settings WHERE key = 'ai_model'")[0][0] == "test/model-b"

    # The next capture runs on the chosen model.
    response = server.http.post(
        "/captures/text", data={"text": "two milks"}, headers={"HX-Request": "true"}
    )
    # 286 when the worker already finished it before the response was rendered.
    assert response.status_code in (200, 286), response.text
    server.wait_for_captures()
    (capture,) = server.query("SELECT status, provider, model FROM captures")
    assert tuple(capture) == ("done", "fake", "test/model-b")

    # Reset brings the .env default back.
    page.locator("#ai-model-reset").tap()
    expect(page.locator("#ai-model-current")).to_contain_text("test/model-a")
    expect(page.locator("#ai-model-source")).to_contain_text("Using AI_MODEL from .env")

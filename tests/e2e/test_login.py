"""The login form: login name + password, in a real browser (Chromium and WebKit)."""

from playwright.sync_api import Page, expect

PASSWORD = "test"  # the e2e server runs with HOUSEHOLD_USERNAME=test, HOUSEHOLD_PASSWORD=test


def test_login_form_has_name_and_password_fields(server, page: Page) -> None:
    page.goto(server.url("/login"))
    name = page.locator("#username")
    expect(name).to_have_attribute("autocomplete", "username")
    expect(name).to_have_attribute("autocapitalize", "none")
    expect(name).to_have_attribute("autocorrect", "off")
    expect(name).to_have_attribute("spellcheck", "false")
    expect(page.locator("#password")).to_have_attribute("autocomplete", "current-password")
    expect(page.get_by_label("Login name")).to_be_visible()
    expect(page.get_by_label("Password")).to_be_visible()


def test_login_with_name_and_password(server, page: Page) -> None:
    page.goto(server.url("/login?next=/settings"))
    page.fill("#username", "Test")  # a capitalised first letter (phones) still matches
    page.fill("#password", PASSWORD)
    page.locator("button[type=submit]").tap()
    page.wait_for_url(server.url("/settings"))
    expect(page.locator("h1")).to_have_text("Settings")


def test_wrong_name_shows_the_generic_error(server, page: Page) -> None:
    page.goto(server.url("/login"))
    page.fill("#username", "someone")
    page.fill("#password", PASSWORD)
    page.locator("button[type=submit]").tap()
    expect(page.get_by_role("alert")).to_have_text("Wrong login name or password.")
    assert page.url.endswith("/login")

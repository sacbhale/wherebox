"""QR deep links through login (AC-10) and scan-to-move (AC-6), in a real browser."""

from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, expect


def test_qr_link_logged_out_goes_through_login_to_the_location(server, page: Page) -> None:
    _, code = server.create_location("Garage")

    page.goto(server.url(f"/l/{code}"))

    parts = urlsplit(page.url)
    assert parts.path == "/login"
    assert parse_qs(parts.query)["next"] == [f"/l/{code}"]
    page.fill("#username", "test")
    page.fill("#password", "test")
    page.locator("button[type=submit]").tap()
    page.wait_for_url(server.url(f"/l/{code}"))
    expect(page.locator("h1")).to_have_text("Garage")
    actions = page.locator(".actions")
    expect(actions.get_by_role("link", name="Capture", exact=True)).to_be_visible()
    expect(actions.get_by_role("link", name="Reconcile", exact=True)).to_be_visible()


def test_scan_to_move_applies_on_arrival_with_undo(server, page: Page, tap) -> None:
    garage_id, garage = server.create_location("Garage")
    kitchen_id, kitchen = server.create_location("Kitchen")
    item_id = server.create_item("Drill", garage_id)
    server.login(page, f"/l/{garage}")

    tap(page.locator(f"#more-{item_id}"))
    tap(page.locator("#sheet-scan"))
    expect(page.locator("#scan-hint")).to_be_visible()
    assert tap.count == 2
    page.locator("#scan-close").tap()
    expect(page.locator("#scan-hint")).to_have_count(0)

    # Scanning the Kitchen tag opens its permanent URL.
    page.goto(server.url(f"/l/{kitchen}"))
    toast = page.locator(f"#toast-move-{item_id}")
    expect(toast).to_contain_text("Drill")
    expect(page.locator(f"#location-items #item-{item_id}")).to_be_visible()
    (row,) = server.query("SELECT location_id FROM items WHERE id = ?", item_id)
    assert row["location_id"] == kitchen_id

    toast.locator("[data-toast-undo]").tap()
    expect(toast).to_have_count(0)
    expect(page.locator(f"#item-{item_id}")).to_have_count(0)
    (row,) = server.query("SELECT location_id FROM items WHERE id = ?", item_id)
    assert row["location_id"] == garage_id

    # The pending move was used up: reopening the Kitchen page moves nothing.
    page.goto(server.url(f"/l/{kitchen}"))
    expect(page.locator(".toast")).to_have_count(0)

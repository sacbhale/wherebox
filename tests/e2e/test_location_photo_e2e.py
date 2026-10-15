"""Location photo, thumbnails and box correction, inline rename, Delete and the collapsible
Tag section on a phone (spec-location-photo; Chromium + WebKit, 390x844): the location page
shows its photo with outlines; a thumbnail opens the item photo view; "Fix outline" by a mouse
drag and by touch pointers saves a new crop that the thumbnail then shows; tapping a name
renames it in place (Enter, no reload); ⋯ → Delete → Confirm delete, then Undo; the Tag
section starts closed and opens. No page-level horizontal scroll, 44px targets."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_mobile_flows import assert_touch_targets

SHELF = Path(__file__).resolve().parents[1] / "fixtures" / "shelf.jpg"
NO_PAGE_SCROLL = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"


def entry(name: str, top: int, left: int = 100) -> dict:
    return {
        "name": name,
        "quantity": 1,
        "category": "Tools",
        "kind": "belonging",
        "confidence": 0.95,
        "boxes": [{"image": 0, "box_2d": [top, left, top + 200, left + 300]}],
    }


STEP = {
    "result": {
        "items": [entry("Hammer", 100), entry("Saw", 600, 550)],
        "overview_image": 0,
        "usage": {"input_tokens": 300, "output_tokens": 90},
    }
}


def page_fits(page: Page) -> None:
    assert page.evaluate(NO_PAGE_SCROLL), f"horizontal page scroll on {page.url}"
    assert_touch_targets(page)


@pytest.fixture
def garage(server, page: Page) -> dict:
    """Garage with a done photo capture (Hammer, Saw boxed) and a manual Rope (no box)."""
    garage_id, code = server.create_location("Garage")
    server.capture_photo("photo", garage_id, SHELF)
    rope = server.create_item("Rope", garage_id)
    ids = {r["name"]: r["id"] for r in server.query("SELECT id, name FROM items")}
    server.login(page, f"/l/{code}")
    return {"id": garage_id, "code": code, "items": ids | {"Rope": rope}}


@pytest.mark.fake_steps(STEP)
def test_location_photo_and_thumbnails(server, page: Page, garage) -> None:
    hammer, saw, rope = (garage["items"][n] for n in ("Hammer", "Saw", "Rope"))
    photo = page.locator("#location-photo img")
    expect(photo).to_be_visible()
    assert photo.evaluate("img => img.complete && img.naturalWidth > 0")
    expect(page.locator("#location-photo .box[data-overview-key]")).to_have_count(2)
    expect(page.locator("#location-photo-all")).to_be_visible()
    expect(page.locator(f"#thumb-{hammer}")).to_be_visible()
    expect(page.locator(f"#thumb-{saw}")).to_be_visible()
    expect(page.locator(f"#thumb-{rope}")).to_have_count(0)
    box = page.locator(f"#thumb-{hammer}").bounding_box()
    assert 47 <= box["width"] <= 49 and 47 <= box["height"] <= 49
    page_fits(page)

    # Tapping an outline finds (and flashes) its row.
    page.locator(f'#location-photo .box[data-overview-key="{saw}"]').click()
    expect(page.locator(f"#item-{saw}")).to_have_class("item flash")


def drag_with_mouse(page: Page, surface, start, end) -> None:
    bounds = surface.bounding_box()
    page.mouse.move(
        bounds["x"] + bounds["width"] * start[0], bounds["y"] + bounds["height"] * start[1]
    )
    page.mouse.down()
    page.mouse.move(
        bounds["x"] + bounds["width"] * end[0], bounds["y"] + bounds["height"] * end[1], steps=6
    )
    page.mouse.up()


def drag_with_touch(page: Page, surface, start, end) -> None:
    """Touch pointer events (pointerType "touch") down, moving, up on the photo. Synthetic
    events do not trigger the browser's native panning, so the page not scrolling here proves
    little: the real check is the computed ``touch-action: none`` asserted in the test."""
    bounds = surface.bounding_box()

    def at(point) -> dict:
        return {
            "pointerId": 7,
            "pointerType": "touch",
            "isPrimary": True,
            "clientX": bounds["x"] + bounds["width"] * point[0],
            "clientY": bounds["y"] + bounds["height"] * point[1],
        }

    surface.dispatch_event("pointerdown", at(start))
    for step in range(1, 5):
        mid = tuple(s + (e - s) * step / 4 for s, e in zip(start, end, strict=True))
        surface.dispatch_event("pointermove", at(mid))
    surface.dispatch_event("pointerup", at(end))


@pytest.mark.fake_steps(STEP)
@pytest.mark.parametrize("pointer", ["mouse", "touch"])
def test_fix_outline_by_dragging_changes_the_thumbnail(server, page: Page, garage, pointer) -> None:
    hammer = garage["items"]["Hammer"]
    before = page.locator(f"#thumb-{hammer}").get_attribute("style")
    page.locator(f"#thumb-{hammer}").tap()
    page.wait_for_url(server.url(f"/items/{hammer}/photo"))
    expect(page.locator("#item-photo .box.is-active")).to_be_visible()
    page_fits(page)

    surface = page.locator("#item-photo .gallery-image")
    assert surface.evaluate("el => getComputedStyle(el).touchAction") != "none"
    page.locator("#fix-outline").tap()
    save = page.locator("#outline-save")
    expect(save).to_be_disabled()
    assert surface.evaluate("el => getComputedStyle(el).touchAction") == "none"
    page_fits(page)

    # Too small first: Save stays disabled. Cancel leaves drawing mode.
    drag = drag_with_mouse if pointer == "mouse" else drag_with_touch
    drag(page, surface, (0.5, 0.5), (0.505, 0.505))
    expect(save).to_be_disabled()
    expect(page.locator("#item-photo-status")).to_contain_text("too small")
    page.locator("#outline-cancel").tap()
    expect(page.locator("#outline-form")).to_be_hidden()
    expect(page.locator(".draw-rect")).to_have_count(0)

    page.locator("#fix-outline").tap()
    scroll = page.evaluate("window.scrollY")
    drag(page, surface, (0.6, 0.55), (0.9, 0.95))
    expect(page.locator(".draw-rect")).to_be_visible()
    assert page.evaluate("window.scrollY") == scroll  # drawing does not scroll the page
    expect(save).to_be_enabled()
    save.tap()
    page.wait_for_url(re.compile(rf".*/items/{hammer}/photo\?photo=\d+&saved=1$"))
    expect(page.locator("#item-photo-status")).to_have_text("Outline saved.")
    assert "saved=1" not in page.url  # dropped from the address after the message shows

    (row,) = server.query(
        "SELECT x0, y0, x1, y1, source FROM item_boxes WHERE item_id = ? AND source = 'user'",
        hammer,
    )
    assert abs(row["x0"] - 0.6) < 0.02 and abs(row["y0"] - 0.55) < 0.02
    assert abs(row["x1"] - 0.9) < 0.02 and abs(row["y1"] - 0.95) < 0.02

    page.locator("#item-photo-back").tap()
    page.wait_for_url(server.url(f"/l/{garage['code']}"))
    after = page.locator(f"#thumb-{hammer}").get_attribute("style")
    assert after != before


@pytest.mark.fake_steps(STEP)
def test_rename_in_place_with_enter(server, page: Page, garage) -> None:
    saw = garage["items"]["Saw"]
    page.evaluate("window.__noReload = true")
    page.locator(f"#name-{saw}").tap()
    field = page.locator(f"#rename-{saw}-input")
    expect(field).to_be_focused()
    selected = field.evaluate("el => el.value.substring(el.selectionStart, el.selectionEnd)")
    assert selected == "Saw"
    page_fits(page)
    field.fill("Hand saw")
    field.press("Enter")
    expect(page.locator(f"#name-{saw}")).to_have_text("Hand saw")
    assert page.evaluate("window.__noReload === true")
    (row,) = server.query("SELECT name FROM items WHERE id = ?", saw)
    assert row["name"] == "Hand saw"

    # Esc cancels; a blank name shows the error under the input.
    page.locator(f"#name-{saw}").tap()
    expect(page.locator(f"#rename-{saw}-input")).to_be_focused()  # the row is wired up
    page.locator(f"#rename-{saw}-input").fill("Nope")
    page.locator(f"#rename-{saw}-input").press("Escape")
    expect(page.locator(f"#name-{saw}")).to_have_text("Hand saw")
    page.locator(f"#name-{saw}").tap()
    expect(page.locator(f"#rename-{saw}-input")).to_be_focused()
    page.locator(f"#rename-{saw}-input").evaluate("el => el.removeAttribute('required')")
    page.locator(f"#rename-{saw}-input").fill("   ")
    page.locator(f"#rename-{saw}-save").tap()
    expect(page.locator(f"#rename-{saw}-message")).to_contain_text("Name is required.")
    assert page.evaluate("window.__noReload === true")


@pytest.mark.fake_steps(STEP)
def test_delete_with_confirm_and_undo(server, page: Page, garage, tap) -> None:
    rope = garage["items"]["Rope"]
    tap(page.locator(f"#more-{rope}"))
    expect(page.locator("#action-sheet")).to_be_visible()
    confirm = page.locator("#sheet-delete-confirm")
    expect(confirm).to_be_hidden()
    tap(page.locator("#sheet-delete-ask"))
    expect(confirm).to_be_visible()
    expect(page.locator("#action-sheet")).to_contain_text("Use this for wrong detections.")
    assert_touch_targets(page)
    tap(confirm)
    expect(page.locator("#action-sheet")).to_have_count(0)
    row = page.locator(f"#item-{rope}")
    expect(row).to_contain_text("Deleted: Rope")
    assert tap.count == 3
    (db,) = server.query("SELECT deleted_reason FROM items WHERE id = ?", rope)
    assert db["deleted_reason"] == "deleted"
    row.get_by_role("button", name="Undo").tap()
    expect(page.locator(f"#more-{rope}")).to_be_visible()
    (db,) = server.query("SELECT deleted_at FROM items WHERE id = ?", rope)
    assert db["deleted_at"] is None


@pytest.mark.fake_steps(STEP)
def test_tag_section_starts_closed_and_opens(server, page: Page, garage) -> None:
    url = page.locator("#permanent-url")
    expect(url).to_be_hidden()
    summary = page.locator("#tag summary")
    expect(summary).to_have_text("Tag: QR code, link and labels")
    assert summary.bounding_box()["height"] >= 43.5
    summary.tap()
    expect(url).to_be_visible()
    expect(page.locator("#tag img.qr")).to_be_visible()
    expect(page.locator("#label-sheet")).to_be_visible()
    page_fits(page)

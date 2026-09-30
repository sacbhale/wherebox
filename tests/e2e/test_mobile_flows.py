"""Quick-action tap counts (AC-6), phone layout (AC-32) and the review queue (AC-7) at an
iPhone 13 viewport in Chromium and WebKit."""

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

SHELF = Path(__file__).resolve().parents[1] / "fixtures" / "shelf.jpg"
VIEWPORT_WIDTH = 390
VIEWPORT_HEIGHT = 844
THUMB_ZONE_TOP = 0.4 * VIEWPORT_HEIGHT  # the bottom 60% of the screen
MIN_TARGET = 43.5  # 44px, allowing for sub-pixel rounding

# Every visible tappable element, measured by its box (a checkbox by its label). Links inside
# a sentence (item meta, muted notes, errors, capture outcomes) are exempt, as in WCAG 2.5.8.
SMALL_TARGETS_JS = """
() => {
  const exempt = ".item-meta, .muted, .error, .capture-status span";
  const found = [];
  for (const el of document.querySelectorAll(
      "a[href], button, input:not([type=hidden]), select, textarea, summary")) {
    if (el.tagName === "A" && el.closest(exempt)) continue;
    const style = getComputedStyle(el);
    if (style.visibility === "hidden") continue;
    const box = (el.type === "checkbox" || el.type === "radio") && el.closest("label")
      ? el.closest("label") : el;
    const r = box.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    if (r.width < MIN || r.height < MIN) {
      found.push(`${el.outerHTML.slice(0, 120)} (${r.width.toFixed(1)}x${r.height.toFixed(1)})`);
    }
  }
  return found;
}
""".replace("MIN", str(MIN_TARGET))


def assert_touch_targets(page: Page) -> None:
    small = page.evaluate(SMALL_TARGETS_JS)
    assert not small, f"targets under 44px on {page.url}:\n" + "\n".join(small)


def bar_top(page: Page) -> float:
    return page.locator(".bottombar").bounding_box()["y"]


def assert_in_thumb_zone(page: Page, selector: str) -> None:
    """``selector`` is fully inside the bottom 60% of the viewport, above the bottom bar
    unless it is part of it, and not covered by anything."""
    element = page.locator(selector)
    box = element.bounding_box()
    assert box is not None, selector
    assert box["y"] >= THUMB_ZONE_TOP, (selector, box)
    in_bar = element.evaluate("el => !!el.closest('.bottombar')")
    limit = VIEWPORT_HEIGHT if in_bar else bar_top(page)
    assert box["y"] + box["height"] <= limit + 0.5, (selector, box, limit)
    center_hits = element.evaluate(
        """el => { const r = el.getBoundingClientRect();
                   const hit = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
                   return el === hit || el.contains(hit); }"""
    )
    assert center_hits, f"{selector} is covered"


@pytest.fixture
def shelf(server, page: Page):
    """Garage > Shelf 1 / Shelf 2, and Kitchen (recently used). Opens Shelf 1 logged in."""
    garage_id, _ = server.create_location("Garage")
    shelf1_id, shelf1 = server.create_location("Shelf 1", garage_id)
    shelf2_id, _ = server.create_location("Shelf 2", garage_id)
    items = {
        name: server.create_item(name, shelf1_id, quantity)
        for name, quantity in [("Batteries", 1), ("Tape", 3), ("Glue", 2), ("Rope", 1)]
    }
    kitchen_id, _ = server.create_location("Kitchen")
    server.create_item("Kettle", kitchen_id)  # makes Kitchen the most recently used location
    server.login(page, f"/l/{shelf1}")
    return {
        "items": items,
        "code": shelf1,
        "garage": garage_id,
        "shelf1": shelf1_id,
        "shelf2": shelf2_id,
        "kitchen": kitchen_id,
    }


def test_plus_and_minus_take_one_tap_and_minus_stops_at_zero(server, page: Page, shelf, tap):
    item_id = shelf["items"]["Batteries"]
    row = page.locator(f"#item-{item_id}")
    qty = row.locator(".qty")

    tap(page.locator(f"#plus-{item_id}"))
    expect(qty).to_have_text("2")
    assert tap.count == 1

    tap.reset()
    tap(page.locator(f"#minus-{item_id}"))
    expect(qty).to_have_text("1")
    assert tap.count == 1

    tap.reset()
    tap(page.locator(f"#minus-{item_id}"))
    expect(qty).to_have_text("0")
    assert tap.count == 1
    expect(page.locator(f"#minus-{item_id}")).to_be_disabled()
    expect(row).to_contain_text("Batteries")  # kept at 0, not removed

    page.reload()
    expect(page.locator(f"#item-{item_id} .qty")).to_have_text("0")
    (db,) = server.query("SELECT quantity, deleted_at FROM items WHERE id = ?", item_id)
    assert (db["quantity"], db["deleted_at"]) == (0, None)


def test_used_up_takes_two_taps_with_undo(server, page: Page, shelf, tap):
    item_id = shelf["items"]["Tape"]

    tap(page.locator(f"#more-{item_id}"))
    tap(page.locator("#action-sheet #sheet-used-up"))
    used_up = page.locator(f"#item-{item_id}.used-up")
    expect(used_up).to_contain_text("Used up: Tape")
    expect(page.locator("#action-sheet")).to_have_count(0)
    assert tap.count == 2

    used_up.get_by_role("button", name="Undo").tap()
    expect(page.locator(f"#item-{item_id} .qty")).to_have_text("3")
    page.reload()
    expect(page.locator(f"#item-{item_id} .qty")).to_have_text("3")


@pytest.mark.parametrize(
    ("target", "name"),
    [("kitchen", "Kitchen"), ("garage", "Garage"), ("shelf2", "Garage › Shelf 2")],
    ids=["recent", "parent", "sibling"],
)
def test_move_takes_two_taps_with_undo(server, page: Page, shelf, tap, target, name):
    item_id = shelf["items"]["Glue"]
    destination = shelf[target]

    tap(page.locator(f"#more-{item_id}"))
    tap(page.locator(f"#move-targets .move-target[data-location-id='{destination}']"))
    moved = page.locator(f"#item-{item_id}.moved")
    expect(moved).to_contain_text(name)
    expect(page.locator("#action-sheet")).to_have_count(0)
    assert tap.count == 2
    (db,) = server.query("SELECT location_id FROM items WHERE id = ?", item_id)
    assert db["location_id"] == destination

    moved.get_by_role("button", name="Undo").tap()
    expect(page.locator(f"#item-{item_id} .qty")).to_have_text("2")
    (db,) = server.query("SELECT location_id FROM items WHERE id = ?", item_id)
    assert db["location_id"] == shelf["shelf1"]


def test_phone_layout_thumb_zone_and_touch_targets(server, page: Page, shelf):
    item_id = shelf["items"]["Batteries"]

    # Location page: the bottom bar's Search and Capture, and the rows' +1/-1.
    for selector in ("#bar-search", "#bar-capture", "#bar-review"):
        assert_in_thumb_zone(page, selector)
    expect(page.locator("#bar-capture")).to_have_attribute("href", f"/l/{shelf['code']}/capture")
    for selector in (f"#plus-{item_id}", f"#minus-{item_id}"):
        page.locator(selector).evaluate("el => el.scrollIntoView({block: 'end'})")
        assert_in_thumb_zone(page, selector)
    expect(page.locator("#capture-photo-input")).to_have_attribute("capture", "environment")
    expect(page.locator("#capture-reconcile-input")).to_have_attribute("capture", "environment")
    assert_touch_targets(page)

    # Nothing at the end of the page hides behind the bar.
    page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
    last = page.locator("main a").last.bounding_box()
    assert last["y"] + last["height"] <= bar_top(page)

    # The action sheet covers the bar and keeps 44px targets.
    page.locator(f"#more-{item_id}").tap()
    expect(page.locator("#action-sheet")).to_be_visible()
    assert_touch_targets(page)
    page.locator("#action-sheet [data-close-sheet]").last.tap()

    # Home: Search and Capture in the bar; the capture panel's camera input.
    page.goto(server.url("/"))
    expect(page.locator("#capture-photo-input")).to_have_attribute("capture", "environment")
    for selector in ("#bar-search", "#bar-capture"):
        assert_in_thumb_zone(page, selector)
    assert_touch_targets(page)

    # The bar's Search opens search; results carry the same +1/-1 rows.
    page.locator("#bar-search").tap()
    page.wait_for_url(server.url("/search"))
    page.fill("input[name=q]", "batteries")
    expect(page.locator(f"#search-results #plus-{item_id}")).to_be_visible()
    page.locator(".filters summary").tap()
    assert_touch_targets(page)

    # The bar's Capture on a location leads to its capture page.
    page.goto(server.url(f"/l/{shelf['code']}"))
    page.locator("#bar-capture").tap()
    page.wait_for_url(server.url(f"/l/{shelf['code']}/capture"))
    expect(page.locator("#capture-photo-input")).to_have_attribute("capture", "environment")
    assert_touch_targets(page)


RECONCILE_STEP = {
    "result": {
        "items": [
            {
                "name": "Hammer",
                "quantity": 5,
                "category": "Tools",
                "kind": "belonging",
                "confidence": 0.95,
                "location_hint": None,
                "price_cents": None,
                "purchase_date": None,
                "matched_existing_id": 1,
            }
        ],
        "receipt": None,
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }
}


@pytest.mark.fake_steps(0, RECONCILE_STEP)
def test_review_queue_actions_remove_rows(server, page: Page, tap):
    garage_id, _ = server.create_location("Garage")
    hammer = server.create_item("Hammer", garage_id, 2)
    rope = server.create_item("Rope", garage_id)
    tent = server.create_item("Tent", garage_id)
    assert hammer == 1  # the reconcile step matches item id 1
    shelf_id, _ = server.create_location("Shelf")
    server.capture_photo("photo", shelf_id, SHELF)  # 2 of 12 items below the threshold
    server.capture_photo("reconcile", garage_id, SHELF)  # Hammer counted 5, Rope/Tent missing
    server.login(page, "/review")

    flagged = page.locator("#review-items .review-row")
    expect(flagged).to_have_count(2)
    missing = page.locator("#review-missing .review-row")
    expect(missing).to_have_count(2)
    counts = page.locator("#review-quantity .review-row")
    expect(counts).to_have_count(1)
    assert_touch_targets(page)

    tap(flagged.first.locator(".review-keep"))
    expect(flagged).to_have_count(1)
    tap(flagged.first.locator(".review-delete"))
    expect(flagged).to_have_count(0)
    tap(page.locator(f"#review-missing .review-row[data-item-id='{rope}'] .review-confirm-missing"))
    expect(missing).to_have_count(1)
    tap(page.locator(f"#review-missing .review-row[data-item-id='{tent}'] .review-still-here"))
    expect(missing).to_have_count(0)
    tap(counts.first.locator(".review-accept"))
    expect(counts).to_have_count(0)
    assert tap.count == 5  # one tap per decision

    page.reload()
    expect(page.locator("#review-empty")).to_be_visible()
    rows = {
        r["id"]: r
        for r in server.query("SELECT id, quantity, deleted_at FROM items WHERE id IN (?, ?, ?)",
                              hammer, rope, tent)
    }  # fmt: skip
    assert rows[hammer]["quantity"] == 5
    assert rows[rope]["deleted_at"] is not None
    assert rows[tent]["deleted_at"] is None


# ---------------------------------------------------------------- home location tree


def toggle(location_id: int) -> str:
    return f"#toggle-{location_id}"


def branch(page: Page, location_id: int):
    return page.locator(f"details.tree-branch[data-location-id='{location_id}']")


def wait_saved(page: Page, location_id: int, is_open: bool) -> None:
    """The async "toggle" event has stored the node's state (before a reload)."""
    page.wait_for_function(
        """([id, open]) => JSON.parse(localStorage.getItem("inventory.openLocations") || "[]")
                             .includes(id) === open""",
        arg=[str(location_id), is_open],
    )


@pytest.fixture
def tree(server, page: Page):
    """Garage > Shelf 1 / Shelf 2 > Blue box, and Kitchen. Opens home logged in."""
    garage_id, garage = server.create_location("Garage")
    server.create_location("Shelf 1", garage_id)
    shelf2_id, _ = server.create_location("Shelf 2", garage_id)
    _, blue = server.create_location("Blue box", shelf2_id)
    server.create_location("Kitchen")
    server.login(page, "/")
    return {"garage_id": garage_id, "garage": garage, "shelf2_id": shelf2_id, "blue": blue}


def test_tree_reaches_nested_location_in_three_taps(server, page: Page, tree, tap):
    blue_link = page.locator(f".tree-link[href='/l/{tree['blue']}']")
    expect(blue_link).to_be_hidden()
    tap(page.locator(toggle(tree["garage_id"])))
    expect(branch(page, tree["garage_id"])).to_have_attribute("open", "")
    tap(page.locator(toggle(tree["shelf2_id"])))
    expect(blue_link).to_be_visible()
    tap(blue_link)
    page.wait_for_url(server.url(f"/l/{tree['blue']}"))
    expect(page.locator("h1")).to_contain_text("Blue box")
    assert tap.count == 3  # 2 expands + 1 tap


def test_tree_name_navigates_and_open_state_survives_reload(server, page: Page, tree):
    # Tapping a collapsed parent's name navigates; it does not toggle.
    page.locator(f".tree-link[href='/l/{tree['garage']}']").tap()
    page.wait_for_url(server.url(f"/l/{tree['garage']}"))

    page.goto(server.url("/"))
    expect(branch(page, tree["garage_id"])).not_to_have_attribute("open", "")
    page.locator(toggle(tree["garage_id"])).tap()
    expect(branch(page, tree["garage_id"])).to_have_attribute("open", "")
    wait_saved(page, tree["garage_id"], True)
    page.reload()
    expect(branch(page, tree["garage_id"])).to_have_attribute("open", "")
    expect(branch(page, tree["shelf2_id"])).not_to_have_attribute("open", "")
    # Collapsing is remembered too.
    page.locator(toggle(tree["garage_id"])).tap()
    expect(branch(page, tree["garage_id"])).not_to_have_attribute("open", "")
    wait_saved(page, tree["garage_id"], False)
    page.reload()
    expect(branch(page, tree["garage_id"])).not_to_have_attribute("open", "")


def test_deep_tree_fits_phone_with_touch_targets(server, page: Page):
    ids, parent = [], None
    for depth in range(1, 9):
        parent, _ = server.create_location(
            f"Level {depth} storage cabinet with a rather long descriptive name", parent
        )
        ids.append(parent)
    _, code = server.create_location("Leaf", parent)
    server.login(page, "/")
    for location_id in ids:
        page.locator(toggle(location_id)).tap()
        expect(branch(page, location_id)).to_have_attribute("open", "")
    deepest = page.locator(f".tree-link[href='/l/{code}']")
    expect(deepest).to_be_visible()
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
    box = deepest.bounding_box()
    assert box["x"] + box["width"] <= VIEWPORT_WIDTH
    assert_touch_targets(page)

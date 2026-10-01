"""Multi-photo capture and annotated views on a phone (spec-multiphoto Part 5; Chromium +
WebKit, 390x844): the photo tray (take 2 one at a time + choose 2, remove 1, send 3; Send
disabled while empty; a 7th photo refused), the numbered gallery with item <-> box
highlighting, "add another angle", the item page's box, the review crop and the comparison
report's model switcher. No page-level horizontal scroll, 44px targets."""

import re
from pathlib import Path

import pytest
from PIL import Image
from playwright.sync_api import Page, Request, expect

from tests.e2e.test_mobile_flows import assert_touch_targets

ACTIVE = re.compile(r"(^|\s)is-active(\s|$)")
NO_PAGE_SCROLL = "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
GOOD = "test/model-b"
WEAK = "openai/gpt-5-nano"


def entry(name: str, *boxes: tuple[int, int], confidence: float = 0.95, **extra) -> dict:
    """A scripted item; ``boxes`` are (image index, top) pairs of 0-1000 boxes."""
    return {
        "name": name,
        "quantity": 1,
        "category": "Tools",
        "kind": "belonging",
        "confidence": confidence,
        "boxes": [{"image": i, "box_2d": [top, 100, top + 250, 500]} for i, top in boxes],
        **extra,
    }


def step(*items: dict) -> dict:
    return {"result": {"items": list(items), "usage": {"input_tokens": 300, "output_tokens": 90}}}


def photos(folder: Path, count: int, seed: int = 0) -> list[str]:
    """``count`` distinct portrait JPEGs (``seed`` makes another set distinct too)."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = folder / f"angle{index}.jpg"
        colour = (30 + 30 * index, 180 - 20 * index, 40 + 60 * seed)
        Image.new("RGB", (480, 640), colour).save(path)
        paths.append(str(path))
    return paths


def page_fits(page: Page) -> None:
    assert page.evaluate(NO_PAGE_SCROLL), f"horizontal page scroll on {page.url}"
    assert_touch_targets(page)


SHELF_STEP = step(
    entry("Hammer", (0, 100), (1, 120)),
    entry("Saw", (0, 500)),
    entry("Pliers", (2, 300)),
    entry("Mystery tin"),
)


@pytest.mark.fake_steps(SHELF_STEP)
def test_tray_sends_three_photos_and_the_gallery_links_items_and_boxes(
    server, page: Page, tmp_path
) -> None:
    _, code = server.create_location("Garage")
    server.login(page, f"/l/{code}")
    files = photos(tmp_path / "shots", 4)
    send = page.locator("#capture-photo-send")
    thumbs = page.locator("#capture-photo-thumbs .tray-thumb")
    expect(send).to_be_disabled()
    expect(send).to_have_text("Send (0 photos)")

    # Two photos from the camera, one at a time, then two chosen together.
    page.set_input_files("#capture-photo-input", files[0])
    page.set_input_files("#capture-photo-input", files[1])
    page.set_input_files("#capture-photo input[multiple]", files[2:])
    expect(thumbs).to_have_count(4)
    expect(send).to_have_text("Send (4 photos)")
    expect(page.locator("#capture-photo-input")).to_have_value("")  # reset after each pick
    page_fits(page)
    page.locator("#capture-photo-thumbs [data-tray-remove]").nth(1).tap()
    expect(thumbs).to_have_count(3)
    expect(send).to_have_text("Send (3 photos)")

    posts: list[Request] = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" else None)
    send.tap()
    status = page.locator("#capture-results .capture-status")
    expect(status).to_have_attribute("data-capture-status", "done", timeout=20_000)
    (post,) = posts
    assert post.url == server.url("/captures/photo")
    assert post.headers["content-type"].startswith("multipart/form-data")
    expect(thumbs).to_have_count(0)  # the tray empties after the send
    expect(send).to_be_disabled()
    (capture,) = server.query("SELECT id FROM captures")
    cid = capture["id"]
    assert len(server.query("SELECT id FROM photos WHERE capture_id = ?", cid)) == 3

    # The result shows the numbered gallery; the unmarked item is listed as such.
    gallery = page.locator(f"#capture-{cid}-gallery")
    expect(gallery.locator(".gallery-frame")).to_have_count(3)
    expect(gallery.locator(".box")).to_have_count(4)
    expect(gallery.locator(".box-label")).to_have_text(["1", "2", "1", "3"])
    ids = {r["name"]: r["id"] for r in server.query("SELECT id, name FROM items")}
    hammer_row = page.locator(f"#capture-{cid}-gallery-item-{ids['Hammer']} .gallery-row")
    expect(hammer_row).to_contain_text("Hammer")
    expect(page.locator(f"#capture-{cid}-gallery-item-{ids['Mystery tin']}")).to_contain_text(
        "not marked"
    )

    # Tap a row: its boxes are highlighted (both photos); tap a box: its row.
    hammer_boxes = gallery.locator(f'.box[data-key="{ids["Hammer"]}"]')
    hammer_row.tap()
    expect(hammer_row).to_have_attribute("aria-pressed", "true")
    expect(hammer_boxes.nth(0)).to_have_class(ACTIVE)
    expect(hammer_boxes.nth(1)).to_have_class(ACTIVE)
    border = hammer_boxes.nth(0).evaluate("el => getComputedStyle(el).borderTopStyle")
    assert border == "dashed"  # not colour alone
    saw_box = gallery.locator(f'.box[data-key="{ids["Saw"]}"]')
    saw_box.click()
    saw_row = page.locator(f"#capture-{cid}-gallery-item-{ids['Saw']} .gallery-row")
    expect(saw_row).to_have_attribute("aria-pressed", "true")
    expect(hammer_row).to_have_attribute("aria-pressed", "false")
    expect(saw_box).to_have_class(ACTIVE)
    # Label numbers stay inside the image.
    for index in range(gallery.locator(".box-label").count()):
        label = gallery.locator(".box-label").nth(index).bounding_box()
        frame = (
            gallery.locator(".box-label")
            .nth(index)
            .locator("xpath=ancestor::div[contains(@class,'gallery-image')]")
            .bounding_box()
        )
        assert label["x"] >= frame["x"] - 0.5 and label["y"] >= frame["y"] - 0.5
        assert label["x"] + label["width"] <= frame["x"] + frame["width"] + 0.5
    page_fits(page)
    page.screenshot(path=str(tmp_path / "gallery.png"), full_page=True)


def test_tray_refuses_a_seventh_photo(server, page: Page, tmp_path) -> None:
    _, code = server.create_location("Garage")
    server.login(page, f"/l/{code}")
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    page.set_input_files("#capture-photo input[multiple]", photos(tmp_path / "seven", 7))
    expect(page.locator("#capture-photo-thumbs .tray-thumb")).to_have_count(6)
    expect(page.locator("#capture-photo-count")).to_contain_text("1 photo not added")
    page.set_input_files("#capture-photo-input", photos(tmp_path / "more", 1))
    expect(page.locator("#capture-photo-thumbs .tray-thumb")).to_have_count(6)
    expect(page.locator("#capture-photo-send")).to_have_text("Send (6 photos)")
    assert posts == []


@pytest.mark.fake_steps(
    step(entry("Hammer", (0, 100)), entry("Saw", (0, 500))),
    step(
        entry("Hammer", (0, 120), (1, 200), matched_existing_id=1),
        entry("Drill", (1, 600), confidence=0.3),
    ),
)
def test_add_another_angle_item_page_and_review_crop(server, page: Page, tmp_path) -> None:
    garage_id, _ = server.create_location("Garage")
    server.capture_photo("photo", garage_id, Path(photos(tmp_path / "root", 1)[0]))
    (root,) = server.query("SELECT id FROM captures")
    rid = root["id"]
    server.login(page, f"/captures/{rid}")
    gallery = page.locator(f"#capture-{rid}-gallery")
    expect(gallery.locator(".gallery-frame")).to_have_count(1)
    expect(page.locator(f"#capture-{rid}-extend-count")).to_have_text("No photos yet (up to 6).")
    page_fits(page)

    send = page.locator(f"#capture-{rid}-extend-send")
    expect(send).to_be_disabled()
    page.set_input_files(
        f"#capture-{rid}-extend-form input[capture]", photos(tmp_path / "x", 1, seed=1)
    )
    send.tap()
    status = page.locator(f"#capture-{rid}-extend-results .capture-status")
    expect(status).to_have_attribute("data-capture-status", "done", timeout=20_000)
    expect(status).to_contain_text("Added 1 new, 1 already known.")

    # The page's gallery was refreshed: two photos; the hammer is boxed on both, the old
    # photo included; the new drill is numbered too.
    expect(gallery.locator(".gallery-frame")).to_have_count(2)
    ids = {r["name"]: r["id"] for r in server.query("SELECT id, name FROM items")}
    assert ids["Hammer"] == 1
    expect(gallery.locator('.box[data-key="1"]')).to_have_count(2)
    expect(gallery.locator(f'.box[data-key="{ids["Drill"]}"]')).to_have_count(1)
    expect(gallery.locator(f'.box[data-key="{ids["Saw"]}"]')).to_have_count(1)
    page_fits(page)

    # The item page shows where the hammer was seen, its box only, highlighted.
    page.goto(server.url("/items/1/edit"))
    seen = page.locator("#item-1-seen")
    expect(seen.locator(".sighting")).to_have_count(2)
    expect(seen.locator(".box.is-active")).to_have_count(2)
    expect(seen.locator(".box").first).to_be_visible()
    expect(seen).to_contain_text("Seen in Garage")
    page_fits(page)
    seen.locator(".sighting-link").first.tap()
    page.wait_for_url(server.url(f"/captures/{rid}?item=1"))
    expect(page.locator(f"#capture-{rid}-gallery-item-1 .gallery-row")).to_have_attribute(
        "aria-pressed", "true"
    )
    expect(page.locator(f'#capture-{rid}-gallery .box.is-active[data-key="1"]')).to_have_count(2)

    # The unsure drill waits in Review with a crop of its box; the crop opens its capture.
    page.goto(server.url("/review"))
    crop = page.locator(f"#review-crop-{ids['Drill']}")
    expect(crop).to_be_visible()
    assert "/photos/" in crop.evaluate("el => getComputedStyle(el).backgroundImage")
    page_fits(page)
    crop.tap()
    page.wait_for_url(server.url(f"/captures/{rid}?item={ids['Drill']}"))
    expect(
        page.locator(f'#capture-{rid}-gallery .box.is-active[data-key="{ids["Drill"]}"]')
    ).to_have_count(1)


@pytest.mark.fake_steps(step(entry("Hammer", (0, 100)), entry("Saw", (0, 500))))
@pytest.mark.fake_models(
    {
        GOOD: {"steps": [step(entry("Hammer", (0, 100)), entry("Saw", (0, 500)))]},
        WEAK: {"steps": [step(entry("Spanner", (0, 300)))]},
    }
)
def test_report_switches_between_the_models_boxes(server, page: Page, tmp_path) -> None:
    garage_id, _ = server.create_location("Garage")
    server.capture_photo("photo", garage_id, Path(photos(tmp_path / "c", 1)[0]))
    (capture,) = server.query("SELECT id FROM captures")
    response = server.http.post(
        "/compare/cases",
        data={"capture_id": str(capture["id"]), "name": ["Hammer", "Saw"], "quantity": ["1", "1"]},
    )
    assert response.status_code == 303, response.text
    (case,) = server.query("SELECT id FROM benchmark_cases")
    response = server.http.post(
        "/compare/runs",
        data={"models": [GOOD, WEAK], "cases": [str(case["id"])]},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200, response.text
    (run,) = server.query("SELECT id FROM comparison_runs")
    server.login(page, f"/compare/runs/{run['id']}")
    expect(page.locator('[data-run-status="done"]')).to_be_visible(timeout=20_000)
    page.reload()

    case_id = case["id"]
    page.locator(f"#case-{case_id} summary").tap()
    gallery = page.locator(f"#case-{case_id}-gallery")
    first, second = (page.locator(f"#case-{case_id}-model-{i}") for i in (0, 1))
    expect(first).to_have_attribute("aria-pressed", "true")
    expect(gallery.locator('.box[data-model="0"]')).to_have_count(2)
    expect(gallery.locator('.box[data-model="0"]').first).to_be_visible()
    expect(gallery.locator('.box[data-model="1"]')).to_be_hidden()
    expect(gallery.locator('.box[data-model="0"] .box-label')).to_have_text(["1", "2"])
    page_fits(page)

    # A highlighted box of the first model is cleared by switching models.
    gallery.locator('.box[data-model="0"]').first.click()
    expect(gallery.locator(".box.is-active")).to_have_count(1)
    second.tap()
    expect(gallery.locator(".box.is-active")).to_have_count(0)
    expect(gallery).not_to_have_class(re.compile(r"\bhas-active\b"))
    expect(second).to_have_attribute("aria-pressed", "true")
    expect(first).to_have_attribute("aria-pressed", "false")
    expect(gallery.locator('.box[data-model="1"]')).to_be_visible()
    expect(gallery.locator('.box[data-model="1"] .box-label')).to_have_text("1")
    expect(gallery.locator('.box[data-model="0"]').first).to_be_hidden()
    page_fits(page)


@pytest.mark.fake_steps(step(entry("Hammer", (0, 100))), step())
def test_tray_follows_the_group_room_and_frees_its_photos(server, page: Page, tmp_path) -> None:
    """After another angle, the tray limit replaced out of band re-renders the tray (room
    for 3, worded as the capture's room); a tray removed by an htmx swap revokes its
    thumbnails' object URLs."""
    garage_id, _ = server.create_location("Garage")
    shots = [Path(p) for p in photos(tmp_path / "root", 6)]
    response = server.http.post(
        "/captures/photo",
        data={"location_id": str(garage_id)},
        files=[("photos", (p.name, p.read_bytes(), "image/jpeg")) for p in shots],
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200, response.text
    server.wait_for_captures()
    (root,) = server.query("SELECT id FROM captures")
    rid = root["id"]
    server.login(page, f"/captures/{rid}")
    count = page.locator(f"#capture-{rid}-extend-count")
    expect(count).to_have_text("No photos yet (up to 6).")

    picker = f"#capture-{rid}-extend-form input[multiple]"
    page.set_input_files(picker, photos(tmp_path / "x", 3, seed=1))
    page.locator(f"#capture-{rid}-extend-send").tap()
    status = page.locator(f"#capture-{rid}-extend-results .capture-status")
    expect(status).to_have_attribute("data-capture-status", "done", timeout=20_000)
    expect(count).to_have_text("No photos yet (this capture has room for 3).")
    expect(page.locator(f"#capture-{rid}-meta")).to_contain_text("9 photos")

    page.set_input_files(picker, photos(tmp_path / "y", 4, seed=2))
    thumbs = page.locator(f"#capture-{rid}-extend-thumbs .tray-thumb")
    expect(thumbs).to_have_count(3)
    expect(count).to_contain_text("1 photo not added: this capture has room for 3.")
    urls = thumbs.locator("img").evaluate_all("imgs => imgs.map(i => i.src)")
    page.evaluate(
        """() => {
          window.__revoked = [];
          const revoke = URL.revokeObjectURL.bind(URL);
          URL.revokeObjectURL = (url) => { window.__revoked.push(url); revoke(url); };
        }"""
    )
    page.evaluate(
        f"""() => htmx.swap("#capture-{rid}-extend",
          '<section id="capture-{rid}-extend"><p>gone</p></section>', {{swapStyle: "outerHTML"}})"""
    )
    expect(page.locator(f"#capture-{rid}-extend-form")).to_have_count(0)
    assert sorted(page.evaluate("() => window.__revoked")) == sorted(urls)

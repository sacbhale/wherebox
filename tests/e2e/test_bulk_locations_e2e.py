"""Bulk add of locations and the 21-up A4 label sheet at an iPhone 13 viewport."""

import io
import re
from urllib.parse import parse_qs, urlsplit

import pytest
import zxingcpp
from PIL import Image
from playwright.sync_api import Page, expect

from tests.e2e.test_compare import NO_PAGE_SCROLL
from tests.e2e.test_mobile_flows import assert_touch_targets

PX_PER_MM = 96 / 25.4
TOLERANCE = 0.5 * PX_PER_MM


def assert_phone_layout(page: Page) -> None:
    assert page.evaluate(NO_PAGE_SCROLL), f"page scrolls sideways on {page.url}"
    assert_touch_targets(page)


def test_bulk_add_a_numbered_series_then_print_labels(server, page: Page):
    garage_id, garage = server.create_location("Garage")
    server.login(page, f"/l/{garage}")

    page.locator("#bulk-add").tap()
    page.wait_for_url(server.url(f"/locations/bulk?parent={garage_id}"))
    expect(page.locator("h1")).to_have_text("Add several locations in Garage")
    assert_phone_layout(page)

    page.fill("#series-stem", "Shelf")
    page.fill("#series-from", "1")
    page.fill("#series-to", "10")
    page.locator("#series-fill").tap()
    names = page.locator("#bulk-names")
    expect(names).to_have_value("\n".join(f"Shelf {n}" for n in range(1, 11)))
    names.fill(names.input_value().replace("Shelf 10", "Top shelf"))
    assert_phone_layout(page)

    page.locator("#bulk-save").tap()
    page.wait_for_url(re.compile(r".*/locations/bulk/done\?.*"))
    done_url = page.url
    expect(page.locator("h1")).to_have_text("Created 10 locations in Garage")
    links = page.locator("#bulk-created a")
    expect(links).to_have_count(10)
    expect(links.last).to_have_text("Top shelf")
    assert_phone_layout(page)
    rows = server.query(
        "SELECT name, short_code FROM locations WHERE parent_id = ? ORDER BY id", garage_id
    )
    assert [row["name"] for row in rows] == [f"Shelf {n}" for n in range(1, 10)] + ["Top shelf"]
    expect(links.first).to_have_attribute("href", f"/l/{rows[0]['short_code']}")

    # The home tree shows the 10 new sub-locations under Garage.
    page.goto(server.url("/"))
    page.locator(f"#toggle-{garage_id}").tap()
    children = page.locator(f'details[data-location-id="{garage_id}"] .tree-link')
    expect(children).to_have_count(10)
    expect(children.filter(has_text="Top shelf")).to_have_count(1)

    # A result link opens the location; "Print labels for these" opens a sheet of 10.
    page.goto(done_url)
    links.first.tap()
    page.wait_for_url(server.url(f"/l/{rows[0]['short_code']}"))
    page.go_back()
    page.locator("#bulk-print").tap()
    page.wait_for_url(re.compile(r".*/labels\?ids=.*"))
    expect(page.locator(".sheet-label")).to_have_count(10)
    expect(page.locator(".sheet-name").last).to_have_text("Top shelf")
    assert_phone_layout(page)


def test_series_helper_pads_and_validates(server, page: Page):
    server.login(page, "/locations/bulk")
    expect(page.locator("h1")).to_have_text("Add several locations")
    page.fill("#bulk-names", "Workbench")
    page.fill("#series-stem", "Bin")
    page.fill("#series-from", "08")
    page.fill("#series-to", "10")
    page.locator("#series-fill").tap()
    expect(page.locator("#bulk-names")).to_have_value("Workbench\nBin 08\nBin 09\nBin 10")
    page.fill("#series-to", "2")
    page.locator("#series-fill").tap()
    expect(page.locator("[data-series-error]")).to_have_text("To must not be less than From.")
    expect(page.locator("#bulk-names")).to_have_value("Workbench\nBin 08\nBin 09\nBin 10")


def test_enter_in_series_fields_adds_to_the_list_not_submit(server, page: Page):
    server.login(page, "/locations/bulk")
    posts = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" else None)
    url = page.url
    expect(page.locator("[data-series]")).to_have_attribute("data-max", "50")
    page.fill("#series-stem", "Bin")
    page.fill("#series-from", "1")
    page.fill("#series-to", "3")
    page.press("#series-to", "Enter")
    expect(page.locator("#bulk-names")).to_have_value("Bin 1\nBin 2\nBin 3")
    assert page.url == url and not posts


@pytest.fixture
def ten_labels(server):
    garage_id, _ = server.create_location("Garage")
    names = "\n".join(f"Shelf {n}" for n in range(1, 11))
    response = server.http.post(
        "/locations/bulk", data={"parent_id": str(garage_id), "names": names}
    )
    assert response.status_code == 303, response.text
    (ids,) = parse_qs(urlsplit(response.headers["location"]).query)["ids"]
    return ids


def first_label_offset_mm(page: Page) -> tuple[float, float, dict]:
    sheet = page.locator(".label-sheet").first.bounding_box()
    label = page.locator(".sheet-label").first.bounding_box()
    return (label["x"] - sheet["x"]) / PX_PER_MM, (label["y"] - sheet["y"]) / PX_PER_MM, label


def test_label_sheet_start_cell_print_geometry_and_pages(
    server, page: Page, ten_labels, browser_name: str
):
    """Geometry in both engines; the PDF page count in Chromium (only it has page.pdf)."""
    server.login(page, f"/labels?ids={ten_labels}")
    expect(page.locator(".sheet-label")).to_have_count(10)
    assert_phone_layout(page)

    page.locator("#cell-3-2").tap()
    page.wait_for_url(re.compile(r".*start_row=3&start_col=2.*"))
    first = page.locator(".sheet-label").first
    expect(first).to_have_attribute("data-row", "3")
    expect(first).to_have_attribute("data-col", "2")
    expect(page.locator("#sheet-map .sheet-cell.used")).to_have_count(7)
    assert_phone_layout(page)

    page.emulate_media(media="print")
    expect(page.locator("#label-controls")).to_be_hidden()
    sheet = page.locator(".label-sheet").first.bounding_box()
    assert abs(sheet["x"]) < 1 and abs(sheet["y"]) < 1  # nothing printed before the sheet
    assert abs(sheet["width"] - 210 * PX_PER_MM) < TOLERANCE
    assert abs(sheet["height"] - 296 * PX_PER_MM) < TOLERANCE  # labels end by 286.85 mm
    x_mm, y_mm, box = first_label_offset_mm(page)
    assert abs(box["width"] - 240) < TOLERANCE and abs(box["height"] - 144) < TOLERANCE
    assert abs(x_mm - 73.25) < 0.5 and abs(y_mm - 91.35) < 0.5
    overflowing = page.evaluate(
        """() => [...document.querySelectorAll('.sheet-label, .sheet-text')]
             .filter(el => el.scrollHeight > el.clientHeight + 1
                        || el.scrollWidth > el.clientWidth + 1).length"""
    )
    assert overflowing == 0

    # The printed QR code scans to the location's permanent URL.
    shot = page.locator(".sheet-label svg").first.screenshot()
    (result,) = zxingcpp.read_barcodes(Image.open(io.BytesIO(shot)))
    (code,) = server.query(
        "SELECT short_code FROM locations WHERE id = ?", int(ten_labels.split(",")[0])
    )
    assert result.text == server.url(f"/l/{code['short_code']}")

    chromium = browser_name == "chromium"
    assert not chromium or pdf_pages(page) == 1
    page.emulate_media(media="screen")
    page.fill("#start-row", "7")
    page.fill("#start-col", "3")
    page.locator("#sheet-apply").tap()
    page.wait_for_url(re.compile(r".*start_row=7.*"))
    expect(page.locator(".label-sheet")).to_have_count(2)
    assert not chromium or pdf_pages(page) == 2


def pdf_pages(page: Page) -> int:
    page.emulate_media(media="print")  # page.pdf() follows an emulated "screen" media
    return len(re.findall(rb"/Type\s*/Page(?!s)", page.pdf(format="A4")))


def test_printer_offset_is_remembered_on_the_device(server, page: Page, ten_labels):
    server.login(page, f"/labels?ids={ten_labels}")
    page.locator(".sheet-form .panel summary").tap()
    page.fill("#offset-dx", "1.5")
    page.fill("#offset-dy", "-0.75")
    page.locator("#sheet-apply").tap()
    page.wait_for_url(re.compile(r".*dx=1\.5.*"))

    page.goto(server.url(f"/labels?ids={ten_labels}"))
    page.wait_for_url(re.compile(r".*dy=-0\.75.*"))
    expect(page.locator("#offset-dx")).to_have_value("1.5")
    page.emulate_media(media="print")
    x_mm, y_mm, _ = first_label_offset_mm(page)
    assert abs(x_mm - (7.25 + 1.5)) < 0.5 and abs(y_mm - (15.15 - 0.75)) < 0.5

    page.emulate_media(media="screen")
    page.locator("#sheet-test").tap()
    page.wait_for_url(re.compile(r".*test=1.*"))
    expect(page.locator(".label-sheet.test .sheet-label")).to_have_count(21)
    expect(page.locator(".label-sheet svg")).to_have_count(0)
    assert_phone_layout(page)


def test_offset_summary_and_reset(server, page: Page, ten_labels):
    server.login(page, f"/labels?ids={ten_labels}")
    expect(page.locator(".sheet-form .panel summary")).to_have_text("Printer offset: none")
    page.locator(".sheet-form .panel summary").tap()
    page.fill("#offset-dx", "1.5")
    page.fill("#offset-dy", "-0.75")
    page.locator("#sheet-apply").tap()
    page.wait_for_url(re.compile(r".*dx=1\.5.*"))
    expect(page.locator(".sheet-form .panel summary")).to_have_text(
        "Printer offset: 1.5 mm right, 0.75 mm up — saved on this device"
    )
    page.locator("#offset-reset").tap()
    page.wait_for_url(re.compile(r".*dx=0&dy=0.*"))
    expect(page.locator(".sheet-form .panel summary")).to_have_text("Printer offset: none")
    page.goto(server.url(f"/labels?ids={ten_labels}"))
    expect(page.locator("#offset-dx")).to_have_value("0")


def add_shelves(server, parent_id: int, numbers: range) -> None:
    names = "\n".join(f"Shelf {n}" for n in numbers)
    response = server.http.post(
        "/locations/bulk", data={"parent_id": str(parent_id), "names": names}
    )
    assert response.status_code == 303, response.text


def test_choose_labels_and_remember_printed_ones(server, page: Page):
    garage_id, garage = server.create_location("Garage")
    add_shelves(server, garage_id, range(1, 11))
    server.login(page, f"/l/{garage}")

    # Never printed: all 10 are ticked and render.
    page.locator("#print-children").tap()
    page.wait_for_url(re.compile(r".*/labels\?ids=.*"))
    boxes = page.locator('#pick-list input[name="pick"]')
    expect(boxes).to_have_count(10)
    for index in range(10):
        expect(boxes.nth(index)).to_be_checked()
    expect(page.locator(".sheet-label")).to_have_count(10)
    expect(page.locator("#pick-list")).to_contain_text("Not printed yet")
    assert_phone_layout(page)

    # The print dialog closing only prompts; nothing is marked until the button is tapped.
    expect(page.locator("#print-check")).to_be_hidden()
    page.evaluate("window.dispatchEvent(new Event('afterprint'))")
    expect(page.locator("#print-check")).to_be_visible()
    expect(page.locator("#mark-printed")).to_have_class(re.compile(r"\bis-prompt\b"))
    assert not server.query("SELECT 1 FROM locations WHERE label_printed_at IS NOT NULL")

    page.locator("#mark-printed").tap()
    page.wait_for_url(re.compile(r".*marked=10.*"))
    expect(page.locator("#labels-marked")).to_have_text("Marked 10 as printed.")
    expect(page.locator("#pick-list")).to_contain_text("Printed ")
    printed = server.query("SELECT count(*) AS n FROM locations WHERE label_printed_at IS NOT NULL")
    assert printed[0]["n"] == 10
    assert_phone_layout(page)

    # Two more shelves: only the new ones are pre-selected.
    add_shelves(server, garage_id, range(11, 13))
    page.goto(server.url(f"/l/{garage}"))
    page.locator("#print-children").tap()
    page.wait_for_url(re.compile(r".*/labels\?ids=.*"))
    expect(boxes).to_have_count(12)
    expect(page.locator('#pick-list input[name="pick"]:checked')).to_have_count(2)
    expect(page.locator(".sheet-label")).to_have_count(2)
    expect(page.locator(".sheet-name")).to_have_text(["Shelf 11", "Shelf 12"])
    assert_phone_layout(page)

    # Ticking one more reloads the sheet with 3 labels, in pool order.
    with page.expect_navigation():
        page.locator("#pick-list label", has_text="Shelf 3").tap()
    expect(page.locator(".sheet-label")).to_have_count(3)
    expect(page.locator(".sheet-name")).to_have_text(["Shelf 3", "Shelf 11", "Shelf 12"])
    assert "pick=" in page.url
    assert_phone_layout(page)

    # The selection survives a tap on the sheet map.
    page.locator("#cell-2-1").tap()
    page.wait_for_url(re.compile(r".*start_row=2.*"))
    expect(page.locator(".sheet-label")).to_have_count(3)

    # Nothing to print or show in the printout but the labels.
    page.emulate_media(media="print")
    expect(page.locator("#pick-panel")).to_be_hidden()
    expect(page.locator("#mark-printed")).to_be_hidden()
    expect(page.locator(".sheet-label").first).to_be_visible()
    page.emulate_media(media="screen")

    with page.expect_navigation():
        page.locator('[data-pick="none"]').tap()
    expect(page.locator("#nothing-selected")).to_have_text(
        "Nothing selected — tick at least one location."
    )
    expect(page.locator(".sheet-label")).to_have_count(0)
    expect(page.locator("#sheet-print")).to_be_disabled()
    assert_phone_layout(page)

    with page.expect_navigation():
        page.locator('[data-pick="unprinted"]').tap()
    expect(page.locator(".sheet-name")).to_have_text(["Shelf 11", "Shelf 12"])


def test_location_page_shows_whether_the_label_was_printed(server, page: Page):
    garage_id, garage = server.create_location("Garage")
    server.login(page, f"/l/{garage}")
    page.locator("#tag summary").tap()
    expect(page.locator("#label-printed")).to_have_text("Label not printed yet")
    response = server.http.post(
        "/labels/printed", data={"ids": str(garage_id), "pick": str(garage_id), "printed": "1"}
    )
    assert response.status_code == 303, response.text
    page.reload()
    page.locator("#tag summary").tap()
    expect(page.locator("#label-printed")).to_have_text(
        re.compile(r"Label printed \d{4}-\d\d-\d\d")
    )

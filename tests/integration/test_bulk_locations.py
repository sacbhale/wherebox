"""Bulk add of locations (``locations.create_many``, /locations/bulk) and the 21-up A4 label
sheet (/labels)."""

import re
from pathlib import Path

import pytest
import zxingcpp
from PIL import Image

from app.inventory import locations, shortcode
from tests.conftest import TEST_PASSWORD, logged_in
from tests.integration.test_auth import PROTECTED
from tests.inventory_helpers import code_of, make_chain, open_db

PUBLIC = "https://inv.example.com:8443"
CODE = re.compile(r"[0-9A-HJKMNP-TV-Z]{6}")
APP_CSS = Path(__file__).resolve().parents[2] / "app" / "static" / "app.css"


def names_under(conn, parent_id):
    return [
        row["name"]
        for row in conn.execute(
            "SELECT name FROM locations WHERE parent_id IS ? AND deleted_at IS NULL ORDER BY id",
            (parent_id,),
        )
    ]


def count_locations(conn):
    return conn.execute("SELECT count(*) FROM locations").fetchone()[0]


# ---------------------------------------------------------------- create_many


def test_create_many_keeps_order_with_unique_codes(conn):
    (garage,) = make_chain(conn, ["Garage"])
    names = ["Workbench", "", "  Tall   cabinet ", "Red bin", "   "]
    result = locations.create_many(conn, names, garage)
    assert names_under(conn, garage) == ["Workbench", "Tall cabinet", "Red bin"]
    assert result.skipped == []
    rows = [locations.get(conn, i) for i in result.created]
    assert [row["name"] for row in rows] == ["Workbench", "Tall cabinet", "Red bin"]
    codes = [row["short_code"] for row in rows]
    assert len(set(codes)) == 3 and all(CODE.fullmatch(c) for c in codes)
    assert all(row["last_used_at"] is None for row in rows)
    assert locations.recent(conn) == []


def test_duplicates_and_existing_siblings_are_skipped_case_insensitively(conn):
    garage, shelf = make_chain(conn, ["Garage", "Shelf 1"])
    gone, _ = locations.create(conn, "Old box", garage)
    locations.delete(conn, gone)
    locations.create(conn, "Shelf 2")  # same name, other parent: not a sibling
    result = locations.create_many(
        conn, ["SHELF 1", "Shelf 2", "shelf 2", "Old box", "Shelf  2", "Shelf 3"], garage
    )
    assert names_under(conn, garage) == ["Shelf 1", "Shelf 2", "Old box", "Shelf 3"]
    new_shelf2 = result.created[0]
    assert result.skipped == [("SHELF 1", shelf), ("shelf 2", new_shelf2), ("Shelf 2", new_shelf2)]


def test_roots_and_inbox_parent_follow_single_create_rules(conn):
    inbox = locations.inbox(conn)["id"]
    result = locations.create_many(conn, ["inbox", "Attic"])
    assert result.skipped == [("inbox", inbox)]
    assert locations.get(conn, result.created[0])["parent_id"] is None
    assert len(locations.create_many(conn, ["Tray"], inbox).created) == 1


@pytest.mark.parametrize("deleted", [False, True])
def test_unknown_or_deleted_parent_is_rejected(conn, deleted):
    (garage,) = make_chain(conn, ["Garage"])
    if deleted:
        locations.delete(conn, garage)
    before = count_locations(conn)
    with pytest.raises(locations.LocationError, match="not found"):
        locations.create_many(conn, ["Shelf"], garage if deleted else 9999)
    assert count_locations(conn) == before


def test_cap_of_50_names(conn):
    before = count_locations(conn)
    with pytest.raises(locations.LocationError, match=r"At most 50 .* 51"):
        locations.create_many(conn, [f"Box {n}" for n in range(51)] + [""])
    assert count_locations(conn) == before
    assert len(locations.create_many(conn, [f"Box {n}" for n in range(50)]).created) == 50


def test_too_long_names_list_their_lines_and_create_nothing(conn):
    before = count_locations(conn)
    names = ["Ok", "", "x" * 201, "Fine", "y" * 300, "z" * 200]
    with pytest.raises(locations.LocationError, match=r"lines 3, 5\)"):
        locations.create_many(conn, names)
    with pytest.raises(locations.LocationError, match="Enter at least one name"):
        locations.create_many(conn, ["", "  "])
    assert count_locations(conn) == before


def test_a_failure_part_way_leaves_nothing_created(conn, monkeypatch):
    (garage,) = make_chain(conn, ["Garage"])
    before = count_locations(conn)
    real = shortcode.insert_with_code
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("disk full")
        return real(*args, **kwargs)

    monkeypatch.setattr(shortcode, "insert_with_code", flaky)
    with pytest.raises(RuntimeError):
        locations.create_many(conn, ["A", "B", "C", "D"], garage)
    assert count_locations(conn) == before


# ---------------------------------------------------------------- bulk routes


@pytest.fixture
def garage(auth_client, auth_settings):
    with open_db(auth_settings) as conn:
        (garage_id,) = make_chain(conn, ["Garage"])
    return garage_id


def test_bulk_routes_are_protected():
    for route in [
        ("GET", "/locations/bulk"),
        ("POST", "/locations/bulk"),
        ("GET", "/locations/bulk/done"),
        ("GET", "/labels"),
    ]:
        assert route in PROTECTED


def test_bulk_form_with_and_without_parent(auth_client, garage):
    page = auth_client.get(f"/locations/bulk?parent={garage}")
    assert page.status_code == 200
    assert "Add several locations in Garage" in page.text
    assert f'name="parent_id" value="{garage}"' in page.text
    assert 'name="names"' in page.text and "data-series" in page.text
    root = auth_client.get("/locations/bulk")
    assert root.status_code == 200 and 'name="parent_id"' not in root.text
    assert auth_client.get("/locations/bulk?parent=9999").status_code == 404
    assert auth_client.get("/locations/bulk?parent=x").status_code == 400


def test_entry_links(auth_client, auth_settings, garage):
    assert 'href="/locations/bulk"' in auth_client.get("/").text
    with open_db(auth_settings) as conn:
        code = code_of(conn, garage)
        shelf, _ = locations.create(conn, "Shelf", garage)
    page = auth_client.get(f"/l/{code}").text
    assert f'href="/locations/bulk?parent={garage}"' in page
    assert f'href="/labels?ids={shelf}"' in page  # print labels for sub-locations
    assert "first 100" not in page
    assert f'href="/labels?ids={garage}"' in page  # add to label sheet


def test_bulk_post_redirects_to_result_with_links_and_skips(auth_client, auth_settings, garage):
    with open_db(auth_settings) as conn:
        existing, _ = locations.create(conn, "Shelf 1", garage)
    response = auth_client.post(
        "/locations/bulk",
        data={"parent_id": str(garage), "names": "shelf 1\nShelf 2\n\nShelf 3\nSHELF 2\n"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    target = response.headers["location"]
    assert target.startswith("/locations/bulk/done?")
    with open_db(auth_settings) as conn:
        assert names_under(conn, garage) == ["Shelf 1", "Shelf 2", "Shelf 3"]
        new = conn.execute(
            "SELECT id, short_code, name FROM locations WHERE parent_id = ? AND id != ? "
            "ORDER BY id",
            (garage, existing),
        ).fetchall()
    done = auth_client.get(target)
    assert done.status_code == 200
    assert "Created 2 locations in Garage" in done.text
    for row in new:
        assert f'<a href="/l/{row["short_code"]}">{row["name"]}</a>' in done.text
    assert "already exists here" in done.text and "listed more than once" in done.text
    ids = ",".join(str(row["id"]) for row in new)
    assert f'href="/labels?ids={ids}"' in done.text
    assert "Print labels for these" in done.text


@pytest.mark.parametrize(
    ("names", "message"),
    [
        ("\n".join(f"Box {n}" for n in range(51)), "At most 50"),
        ("Ok\n" + "x" * 201, "line 2"),
        ("\n \n", "Enter at least one name"),
    ],
)
def test_bulk_post_errors_are_400_and_create_nothing(
    auth_client, auth_settings, garage, names, message
):
    with open_db(auth_settings) as conn:
        before = count_locations(conn)
    response = auth_client.post("/locations/bulk", data={"parent_id": str(garage), "names": names})
    assert response.status_code == 400
    assert message in response.text
    assert 'id="bulk-names"' in response.text  # the form again, keeping what was typed
    with open_db(auth_settings) as conn:
        assert count_locations(conn) == before


def test_bulk_post_to_missing_parent_is_400(auth_client):
    response = auth_client.post("/locations/bulk", data={"parent_id": "9999", "names": "A"})
    assert response.status_code == 400


def test_bulk_post_is_origin_checked(auth_client, auth_settings, garage):
    response = auth_client.post(
        "/locations/bulk",
        data={"parent_id": str(garage), "names": "Shelf"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403
    with open_db(auth_settings) as conn:
        assert names_under(conn, garage) == []


def test_huge_numbers_are_400_not_500(lan_client):
    huge = "99999999999999999999"
    assert lan_client.get("/labels", params={"ids": huge}).status_code == 400
    assert lan_client.get("/locations/bulk/done", params={"ids": huge}).status_code == 400
    assert lan_client.get("/locations/bulk/done", params={"skipped": huge}).status_code == 400
    assert lan_client.get("/locations/bulk", params={"parent": huge}).status_code == 400
    assert lan_client.get("/locations/bulk", params={"parent": str(2**63)}).status_code == 400
    assert lan_client.get("/locations/bulk", params={"parent": "9" * 5000}).status_code == 400


def test_bulk_post_rejects_a_huge_textarea(auth_client):
    response = auth_client.post("/locations/bulk", data={"names": "a\n" * 10_001})
    assert response.status_code == 400 and "too long" in response.text


def test_bulk_done_validates_ids(auth_client):
    assert auth_client.get("/locations/bulk/done?ids=1,x").status_code == 400
    ids = ",".join(str(n) for n in range(101))
    assert auth_client.get(f"/locations/bulk/done?ids={ids}").status_code == 400


# ---------------------------------------------------------------- label sheet


@pytest.fixture
def lan_client(make_settings):
    settings = make_settings(household_password=TEST_PASSWORD, public_base_url=PUBLIC)
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            (garage,) = make_chain(conn, ["Garage"])
            client.ids = locations.create_many(
                conn, [f"Shelf {n}" for n in range(1, 11)], garage
            ).created
            client.codes = [code_of(conn, i) for i in client.ids]
            client.settings = settings
        yield client


def rasterize(svg: str, scale: int = 8) -> Image.Image:
    """Draw segno's inline SVG (a viewBox in modules; one 1-module stroke per dark run)."""
    size = int(re.search(r'viewBox="0 0 (\d+) \d+"', svg).group(1))
    image = Image.new("L", (size, size), 255)
    x = y = 0.0
    for command, args in re.findall(r"([Mmh])([^Mmh]*)", re.search(r' d="([^"]+)"', svg)[1]):
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", args)]
        if command == "M":
            x, y = numbers
        elif command == "m":
            x, y = x + numbers[0], y + numbers[1]
        else:
            for column in range(int(x), int(x + numbers[0])):
                image.putpixel((column, int(y)), 0)
            x += numbers[0]
    return image.resize((size * scale, size * scale), Image.Resampling.NEAREST)


def label_cells(html: str) -> list[tuple[int, int]]:
    return [(int(r), int(c)) for r, c in re.findall(r'data-row="(\d)" data-col="(\d)"', html)]


def test_label_sheet_qr_decodes_to_the_public_url(lan_client):
    page = lan_client.get("/labels", params={"ids": ",".join(map(str, lan_client.ids))})
    assert page.status_code == 200
    svgs = re.findall(r"<svg.*?</svg>", page.text, re.DOTALL)
    assert len(svgs) == 10
    for index in (0, 9):
        (result,) = zxingcpp.read_barcodes(rasterize(svgs[index]))
        assert result.format == zxingcpp.BarcodeFormat.QRCode
        assert result.text == f"{PUBLIC}/l/{lan_client.codes[index]}"
    assert f'<p class="sheet-url">inv.example.com:8443/l/{lan_client.codes[0]}</p>' in page.text
    assert '<p class="sheet-path">Garage</p>' in page.text
    assert '<p class="sheet-name">Shelf 1</p>' in page.text


def test_label_sheet_start_position_and_sheets(lan_client):
    ids = ",".join(map(str, lan_client.ids))
    page = lan_client.get("/labels", params={"ids": ids, "start_row": 3, "start_col": 2}).text
    cells = label_cells(page)
    assert cells[0] == (3, 2) and len(cells) == 10
    assert 'style="left: 73.25mm; top: 91.35mm"' in page
    assert page.count('class="label-sheet"') == 1
    assert page.count("sheet-cell used") == 7 and page.count("sheet-cell print") == 10
    assert 'id="cell-3-2"' in page and "aria-current" in page

    late = lan_client.get("/labels", params={"ids": ids, "start_row": 7, "start_col": 3}).text
    assert late.count('class="label-sheet"') == 2
    assert label_cells(late)[:2] == [(7, 3), (1, 1)]
    assert "10 labels on 2 sheets" in late

    shifted = lan_client.get("/labels", params={"ids": ids, "dx": "-1.25", "dy": "0.5"}).text
    assert 'style="left: 6.0mm; top: 15.65mm"' in shifted


def test_label_sheet_test_mode_prints_outlines_only(lan_client):
    page = lan_client.get("/labels", params={"ids": lan_client.ids[0], "test": "1"}).text
    assert page.count('class="sheet-label"') == 21
    assert "<svg" not in page
    assert 'class="label-sheet test"' in page


def test_test_sheet_builds_no_qr_and_keeps_the_start_cell(lan_client, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("QR built for the test sheet")

    monkeypatch.setattr("app.inventory.routes.qr.svg_inline", boom)
    ids = lan_client.ids[0]
    page = lan_client.get(
        "/labels", params={"ids": ids, "test": "1", "start_row": 3, "start_col": 2}
    ).text
    assert 'name="start_row" value="3"' in page and 'name="start_col" value="2"' in page
    assert f"/labels?ids={ids}&amp;dx=0&amp;dy=0&amp;start_row=3&amp;start_col=2" in page
    monkeypatch.undo()
    normal = lan_client.get("/labels", params={"ids": ids, "start_row": 3, "start_col": 2}).text
    assert "start_row=3&amp;start_col=2&amp;test=1" in normal


def test_offset_summary_in_words(lan_client):
    ids = lan_client.ids[0]
    none = lan_client.get("/labels", params={"ids": ids}).text
    assert (
        "Printer offset: none" in none and "saved on this device" not in none.split("</summary>")[0]
    )
    page = lan_client.get("/labels", params={"ids": ids, "dx": "1.5", "dy": "-0.75"}).text
    assert "Printer offset: 1.5 mm right, 0.75 mm up — saved on this device" in page
    assert "data-offset-reset" in page


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"ids": ""},
        {"ids": "1,x"},
        {"ids": "-1"},
        {"ids": ",".join(str(n) for n in range(1, 102))},
        {"ids": "1", "start_row": "0"},
        {"ids": "1", "start_row": "8"},
        {"ids": "1", "start_col": "4"},
        {"ids": "1", "start_col": "x"},
        {"ids": "1", "dx": "5.5"},
        {"ids": "1", "dy": "0.1"},
        {"ids": "1", "dx": "nan"},
    ],
)
def test_label_sheet_rejects_bad_params(lan_client, params):
    assert lan_client.get("/labels", params=params).status_code == 400


def test_label_sheet_skips_unknown_and_deleted(lan_client):
    with open_db(lan_client.settings) as conn:
        locations.delete(conn, lan_client.ids[1])
    ids = f"{lan_client.ids[0]},{lan_client.ids[1]},99999,{lan_client.ids[2]},{lan_client.ids[0]}"
    page = lan_client.get("/labels", params={"ids": ids}).text
    assert re.findall(r'<p class="sheet-name">([^<]+)</p>', page) == ["Shelf 1", "Shelf 3"]
    assert lan_client.get("/labels", params={"ids": "99999"}).status_code == 404
    hundred = ",".join(str(n) for n in range(1, 101))
    assert lan_client.get("/labels", params={"ids": hundred}).status_code == 200


def test_label_sheet_print_css(lan_client):
    page = lan_client.get("/labels", params={"ids": lan_client.ids[0]}).text
    assert "@page { size: A4; margin: 0; }" in page
    assert 'class="no-print" id="label-controls"' in page
    css = APP_CSS.read_text()
    assert "width: 210mm; height: 297mm" in css  # on screen
    assert ".label-sheet { height: 296mm; outline: 0; margin: 0; break-after: page; }" in css
    assert "width: 63.5mm; height: 38.1mm" in css
    assert ".label-sheet:last-child { break-after: auto; }" in css


def test_children_labels_print_in_creation_order_and_cap_at_100(auth_client, auth_settings, garage):
    with open_db(auth_settings) as conn:
        code = code_of(conn, garage)
        ids = locations.create_many(conn, [f"Shelf {n}" for n in range(1, 11)], garage).created
    page = auth_client.get(f"/l/{code}").text
    assert f'href="/labels?ids={",".join(map(str, ids))}"' in page
    with open_db(auth_settings) as conn:
        locations.create_many(conn, [f"Bin {n}" for n in range(1, 51)], garage)
        locations.create_many(conn, [f"Box {n}" for n in range(1, 51)], garage)
    page = auth_client.get(f"/l/{code}").text
    assert "(first 100 of 110)" in page


# ---------------------------------------------------------------- choosing labels


def sheet_names(html: str) -> list[str]:
    return re.findall(r'<p class="sheet-name">([^<]+)</p>', html)


def printed_at(settings, location_id):
    with open_db(settings) as conn:
        return conn.execute(
            "SELECT label_printed_at FROM locations WHERE id = ?", (location_id,)
        ).fetchone()[0]


def test_set_label_printed_marks_live_ids_only(conn):
    (garage,) = make_chain(conn, ["Garage"])
    shelf1, shelf2, gone = locations.create_many(conn, ["S1", "S2", "S3"], garage).created
    locations.delete(conn, gone)
    assert locations.set_label_printed(conn, [shelf1, gone, 9999, shelf1], True) == 1
    assert locations.get(conn, shelf1)["label_printed_at"] is not None
    assert locations.get(conn, shelf2)["label_printed_at"] is None
    assert locations.set_label_printed(conn, [shelf1, shelf2], False) == 2
    assert locations.get(conn, shelf1)["label_printed_at"] is None
    with pytest.raises(locations.LocationError, match="At most 100"):
        locations.set_label_printed(conn, list(range(1, 102)), True)


def test_mark_printed_route_is_protected():
    assert ("POST", "/labels/printed") in PROTECTED


def test_pick_accepts_commas_and_repeated_values_and_ignores_ids_outside_the_pool(lan_client):
    ids = lan_client.ids
    pool = ",".join(map(str, ids[:5]))
    comma = lan_client.get("/labels", params={"ids": pool, "pick": f"{ids[0]},{ids[2]}"}).text
    repeated = lan_client.get(
        "/labels", params=[("ids", pool), ("pick", ""), ("pick", ids[0]), ("pick", ids[2])]
    ).text
    assert sheet_names(comma) == sheet_names(repeated) == ["Shelf 1", "Shelf 3"]
    outside = lan_client.get(
        "/labels", params={"ids": pool, "pick": f"{ids[1]},{ids[7]},99999"}
    ).text
    assert sheet_names(outside) == ["Shelf 2"]
    for bad in ("1,x", "-1", "99999999999999999999", ",".join(str(n) for n in range(101))):
        assert lan_client.get("/labels", params={"ids": pool, "pick": bad}).status_code == 400


def test_default_selection_is_the_unprinted_labels_or_all(lan_client):
    ids = lan_client.ids
    pool = ",".join(map(str, ids))
    with open_db(lan_client.settings) as conn:
        locations.set_label_printed(conn, ids[:8], True)
    page = lan_client.get("/labels", params={"ids": pool}).text
    assert sheet_names(page) == ["Shelf 9", "Shelf 10"]
    assert "2 labels on 1 sheet" in page and "Choose labels: 2 of 10 selected" in page
    assert page.count("Not printed yet") == 2 and page.count("Printed 20") == 8
    assert page.count("checked") == 2
    assert re.search(r'id="pick-panel"\s*open>', page)
    with open_db(lan_client.settings) as conn:
        locations.set_label_printed(conn, ids, True)
    assert len(sheet_names(lan_client.get("/labels", params={"ids": pool}).text)) == 10
    single = lan_client.get("/labels", params={"ids": ids[0]}).text  # "Add to label sheet"
    assert sheet_names(single) == ["Shelf 1"]
    assert re.search(r'id="pick-panel"\s*>', single)  # closed for a single location


def test_empty_pick_selects_nothing(lan_client):
    page = lan_client.get("/labels", params={"ids": ",".join(map(str, lan_client.ids)), "pick": ""})
    assert page.status_code == 200
    assert "Nothing selected — tick at least one location" in page.text
    assert 'class="label-sheet"' not in page.text and "sheet-cell print" not in page.text
    assert re.search(r'<button type="button" id="sheet-print" data-print\s+disabled>', page.text)
    assert 'id="mark-printed"' not in page.text
    assert "checked" not in page.text


def test_labels_render_in_pool_order_only_for_picked(lan_client):
    ids = lan_client.ids
    pick = f"{ids[5]},{ids[1]},{ids[3]}"
    page = lan_client.get("/labels", params={"ids": ",".join(map(str, ids)), "pick": pick}).text
    assert sheet_names(page) == ["Shelf 2", "Shelf 4", "Shelf 6"]
    assert label_cells(page) == [(1, 1), (1, 2), (1, 3)]
    assert "3 labels on 1 sheet" in page


def test_every_link_carries_the_pick(lan_client):
    ids = lan_client.ids
    pool = f"{ids[0]},{ids[1]},{ids[2]}"
    pick = f"{ids[0]},{ids[2]}"
    encoded = f"pick={ids[0]}%2C{ids[2]}"
    page = lan_client.get("/labels", params={"ids": pool, "pick": pick}).text
    hrefs = re.findall(r'href="(/labels\?[^"]+)"', page)
    assert len(hrefs) == 22  # 21 sheet-map cells and the test sheet
    assert all(href.endswith(encoded) for href in hrefs if "test=1" not in href)
    assert f"test=1&amp;{encoded}" in page
    assert f'name="pick" value="{ids[1]}" form="sheet-form"' in page
    assert re.search(rf'name="pick" value="{ids[0]}" form="sheet-form"\s+checked', page)
    test = lan_client.get("/labels", params={"ids": pool, "pick": pick, "test": "1"}).text
    assert f'<input type="hidden" name="pick" value="{pick}">' in test
    (back,) = re.findall(r'href="(/labels\?[^"]+)"', test)
    assert back.endswith(encoded)
    default = lan_client.get("/labels", params={"ids": pool}).text  # the default, made explicit
    assert f"start_col=1&amp;pick={ids[0]}%2C{ids[1]}%2C{ids[2]}" in default


def test_mark_printed_and_not_printed(lan_client):
    ids = lan_client.ids
    pool = ",".join(map(str, ids))
    page = lan_client.get("/labels", params={"ids": pool, "pick": f"{ids[0]},{ids[1]}"}).text
    assert 'action="/labels/printed"' in page and "Mark these 2 as printed" in page
    assert "Mark as not printed" in page
    with open_db(lan_client.settings) as conn:
        locations.delete(conn, ids[2])
    form = {
        "ids": pool,
        "pick": f"{ids[0]},{ids[1]},{ids[2]}",
        "start_row": "3",
        "start_col": "2",
        "dx": "0.5",
        "dy": "0",
        "printed": "1",
    }
    response = lan_client.post("/labels/printed", data=form, follow_redirects=False)
    assert response.status_code == 303
    target = response.headers["location"]
    assert target.startswith("/labels?") and "pick" not in target
    assert "start_row=3" in target and "dx=0.5" in target and "marked=2" in target
    assert printed_at(lan_client.settings, ids[0]) and printed_at(lan_client.settings, ids[1])
    assert printed_at(lan_client.settings, ids[2]) is None  # deleted: ignored
    after = lan_client.get(target).text
    assert "Marked 2 as printed." in after
    assert sheet_names(after) == [f"Shelf {n}" for n in range(4, 11)]  # the rest, unprinted

    response = lan_client.post(
        "/labels/printed", data=form | {"pick": str(ids[0]), "printed": "0"}, follow_redirects=False
    )
    assert response.status_code == 303 and "unmarked=1" in response.headers["location"]
    assert printed_at(lan_client.settings, ids[0]) is None
    assert "Marked 1 as not printed." in lan_client.get(response.headers["location"]).text


def test_mark_printed_rejects_cross_origin_and_bad_ids(lan_client):
    ids = lan_client.ids
    data = {"ids": str(ids[0]), "pick": str(ids[0]), "printed": "1"}
    response = lan_client.post(
        "/labels/printed", data=data, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert printed_at(lan_client.settings, ids[0]) is None
    too_many = ",".join(str(n) for n in range(1, 102))
    bad_forms = (
        data | {"pick": too_many},
        data | {"pick": ""},
        data | {"pick": "1,x"},
        data | {"printed": "yes"},
        {"ids": str(ids[0]), "pick": str(ids[0])},  # no printed value
        data | {"pick": str(ids[1])},  # outside the pool
    )
    for bad in bad_forms:
        assert lan_client.post("/labels/printed", data=bad).status_code == 400
    assert printed_at(lan_client.settings, ids[0]) is None
    assert printed_at(lan_client.settings, ids[1]) is None


def test_location_page_shows_label_printed_status(lan_client):
    code = lan_client.codes[0]
    assert "Label not printed yet" in lan_client.get(f"/l/{code}").text
    with open_db(lan_client.settings) as conn:
        locations.set_label_printed(conn, [lan_client.ids[0]], True)
    page = lan_client.get(f"/l/{code}").text
    assert re.search(r'id="label-printed">Label printed \d{4}-\d{2}-\d{2}<', page)


def test_single_label_page_does_not_mark(lan_client):
    assert lan_client.get(f"/l/{lan_client.codes[0]}/label").status_code == 200
    assert printed_at(lan_client.settings, lan_client.ids[0]) is None


def test_selection_controls_are_screen_only(lan_client):
    page = lan_client.get("/labels", params={"ids": ",".join(map(str, lan_client.ids))}).text
    controls = page.split('<div class="label-sheets">')[0]
    for marker in ('id="pick-panel"', 'id="mark-form"', 'id="print-check"'):
        assert marker in controls.split('class="no-print" id="label-controls"', 1)[1]

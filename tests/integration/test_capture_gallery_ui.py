"""Annotated views (spec-multiphoto Part 5, R11-R16): the photo tray markup, the capture
group page /captures/{id} (gallery with % boxes numbered like the item list, moved / used-up
/ not-marked items, add another angle with capacity or refusal), the panel result and the
/captures list, the item page's "Where it was seen", review crops and the comparison
report's model switcher. Styles carry numbers only, never model text."""

import re
from datetime import UTC, datetime

import pytest

from app.ai import openrouter_catalog
from app.ai.fake import FakeProvider
from app.capture import boxes, groups
from app.compare import cases, execution, runs
from app.compare.scoring import Expected
from app.inventory import items, locations
from tests.capture_helpers import (
    FIXTURES,
    capture_id,
    capture_items,
    item,
    make_jpegs,
    post_extend,
    post_photos,
    post_receipt,
    post_text,
    result,
    run,
)
from tests.compare_helpers import CATALOG
from tests.conftest import TEST_PASSWORD, logged_in
from tests.integration.test_auth import PROTECTED
from tests.inventory_helpers import open_db

HOSTILE = 'Tape"; background:url(javascript:alert(1)) </style><b>'
STYLE = re.compile(r'style="([^"]*)"')
# Every style attribute the gallery, crops and report write: numbers, %, and photo URLs.
NUMERIC_STYLE = re.compile(
    r"^(?:(?:left|top|width|height):-?\d+(?:\.\d+)?%;?"
    r"|aspect-ratio:\d+/\d+"
    r"|background-image:url\('/photos/\d+'\);?"
    r"|background-(?:size|position):\d+(?:\.\d+)?% \d+(?:\.\d+)?%;?)+$"
)


def box(image: int, top: int = 100, left: int = 100) -> dict[str, object]:
    return {"image": image, "box_2d": [top, left, top + 200, left + 300]}


@pytest.fixture
def places(auth_client, auth_settings) -> dict[str, int]:
    with open_db(auth_settings) as conn:
        garage, _ = locations.create(conn, "Garage")
        shed, _ = locations.create(conn, "Shed")
    return {"garage": garage, "shed": shed}


@pytest.fixture
def capture(auth_client, auth_settings, places, tmp_path) -> dict[str, object]:
    """A done 2-photo capture in the Garage: Tape (photo 1), Glue (photos 1 and 2), Rake
    (photo 2, then moved to the Shed), Soap (photo 1, then used up) and Screws (no box)."""
    cid = capture_id(post_photos(auth_client, places["garage"], make_jpegs(tmp_path / "r", 2)))
    answer = result(
        [
            item("Screws", quantity=10),
            item(HOSTILE, boxes=[box(0, 100, 100)]),
            item("Glue", boxes=[box(0, 500, 500), box(1, 100, 100)]),
            item("Rake", boxes=[box(1, 600, 600)]),
            item("Soap", boxes=[box(0, 700, 50)]),
        ]
    )
    assert run(auth_settings, FakeProvider([answer]))
    with open_db(auth_settings) as conn:
        ids = {r["name"]: r["id"] for r in capture_items(conn, cid)}
        items.move_item(conn, ids["Rake"], places["shed"])
        items.use_up(conn, ids["Soap"])
        photo_ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM photos WHERE capture_id = ? ORDER BY position", (cid,)
            )
        ]
    return {"id": cid, "items": ids, "photos": photo_ids}


def assert_numeric_styles(html: str) -> None:
    styles = STYLE.findall(html)
    assert styles
    for style in styles:
        assert NUMERIC_STYLE.match(style), style


def gallery_rows(html: str, prefix: str) -> dict[int, tuple[int | None, str]]:
    """{item id: (number or None, row html)} of a gallery's item list."""
    rows = {}
    for match in re.finditer(
        rf'<li class="gallery-item state-(\w+)" id="{prefix}-item-(\d+)">(.*?)</li>', html, re.S
    ):
        number = re.search(r'<span class="gallery-num">(\d+)</span>', match.group(3))
        rows[int(match.group(2))] = (int(number.group(1)) if number else None, match.group(0))
    return rows


def _attr(tag: str, name: str) -> str | None:
    found = re.search(rf'\b{name}="([^"]*)"', tag)
    return found.group(1) if found else None


def boxes_by_key(html: str) -> dict[str, list[tuple[str | None, str]]]:
    """{data-key: [(label number, style)]} of the boxes on a page (any attribute order)."""
    found: dict[str, list[tuple[str | None, str]]] = {}
    for match in re.finditer(r"<span\b[^>]*>", html):
        tag = match.group(0)
        key, style = _attr(tag, "data-key"), _attr(tag, "style")
        if "box" not in (_attr(tag, "class") or "").split() or key is None or style is None:
            continue
        label = re.match(r'<span class="box-label[^"]*">(\d+)</span>', html[match.end() :])
        found.setdefault(key, []).append((label.group(1) if label else None, style))
    return found


def active_box_keys(html: str) -> list[str]:
    """data-key of every box whose class list contains is-active (any attribute order)."""
    keys = []
    for tag in re.findall(r"<span\b[^>]*>", html):
        key = _attr(tag, "data-key")
        if key is not None and {"box", "is-active"} <= set((_attr(tag, "class") or "").split()):
            keys.append(key)
    return keys


def test_new_routes_are_protected():
    assert ("GET", "/captures/1") in PROTECTED
    assert ("POST", "/captures/1/extend") in PROTECTED


def test_photo_forms_use_the_tray(auth_client, places):
    html = auth_client.get("/captures/panel", params={"loc": places["garage"]}).text
    for form_id in ("capture-photo", "capture-reconcile"):
        form = re.search(rf'<form[^>]*id="{form_id}".*?</form>', html, re.S).group(0)
        assert "data-tray" in form and 'hx-trigger="change' not in form
        inputs = re.findall(r'<input type="file"[^>]*>', form)
        assert len(inputs) == 2
        assert all("name=" not in tag for tag in inputs)  # files go through the tray only
        take, choose = inputs
        assert 'capture="environment"' in take and "multiple" not in take
        assert "multiple" in choose and "capture=" not in choose
        send = re.search(r'<button type="submit"[^>]*data-tray-send[^>]*>([^<]*)<', form)
        assert send and " disabled" in send.group(0) and send.group(1) == "Send (0 photos)"
        assert f'data-error-target="#{form_id}-message"' in form
        assert f'id="{form_id}-message"' in form
        assert 'data-tray-max="6"' in form


def test_capture_page_renders_the_annotated_gallery(auth_client, capture):
    cid, ids, photos = capture["id"], capture["items"], capture["photos"]
    response = auth_client.get(f"/captures/{cid}")
    assert response.status_code == 200
    html = response.text
    prefix = f"capture-{cid}-gallery"
    assert f'id="{prefix}"' in html and "data-gallery" in html
    assert f'id="{prefix}-photo-{photos[0]}"' in html and f'id="{prefix}-photo-{photos[1]}"' in html
    assert 'src="/photos/' in html and "aspect-ratio:320/240" in html

    rows = gallery_rows(html, prefix)
    boxes = boxes_by_key(html)
    # Boxed items are numbered 1..4 by their first photo; Screws (no box) is unnumbered.
    numbered = {item_id: number for item_id, (number, _row) in rows.items() if number}
    assert sorted(numbered.values()) == [1, 2, 3, 4]
    assert rows[ids["Screws"]][0] is None and "not marked" in rows[ids["Screws"]][1]
    assert str(ids["Screws"]) not in boxes
    for item_id, number in numbered.items():
        assert {label for label, _style in boxes[str(item_id)]} == {str(number)}
        assert f'data-key="{item_id}"' in rows[item_id][1]
        assert 'aria-pressed="false"' in rows[item_id][1]
    assert len(boxes[str(ids["Glue"])]) == 2  # one per photo
    # box_2d [100, 100, 300, 400] (0-1000, ymin xmin ymax xmax) -> % of the image.
    # Soap (x0 0.05) comes before it on photo 1, so it is number 2.
    assert boxes[str(ids[HOSTILE])] == [("2", "left:10.00%;top:10.00%;width:30.00%;height:20.00%")]

    assert "moved to Shed" in rows[ids["Rake"]][1] and "state-moved" in rows[ids["Rake"]][1]
    assert "used up" in rows[ids["Soap"]][1] and "/edit" not in rows[ids["Soap"]][1]
    assert all(
        'aria-hidden="true"' in tag for tag in re.findall(r'<span class="box[ "][^>]*>', html)
    )
    # The model's text is escaped and never reaches a style attribute.
    assert HOSTILE not in html and "&lt;b&gt;" in html
    assert_numeric_styles(html)


def test_capture_page_highlights_the_item_from_the_query(auth_client, capture):
    cid, glue = capture["id"], capture["items"]["Glue"]
    html = auth_client.get(f"/captures/{cid}", params={"item": glue}).text
    assert "has-active" in html and "data-scroll-active" in html
    assert active_box_keys(html) == [str(glue), str(glue)]
    assert re.search(rf'data-key="{glue}"\s+aria-pressed="true"', html)


def test_an_extension_id_shows_the_whole_group(auth_client, auth_settings, capture, tmp_path):
    cid = capture["id"]
    ext = capture_id(post_extend(auth_client, cid, make_jpegs(tmp_path / "e", 1, (300, 200))))
    tape = capture["items"][HOSTILE]
    assert run(
        auth_settings,
        FakeProvider(
            [result([item("Tape", matched_existing_id=tape, boxes=[box(2)]), item("Saw")])]
        ),
    )
    with open_db(auth_settings) as conn:
        assert groups.extension_outcome(conn, ext) == (1, 1)
    html = auth_client.get(f"/captures/{ext}").text
    assert f'id="capture-{cid}-gallery"' in html
    assert html.count(f'id="capture-{cid}-gallery-photo-') == 3
    assert "Saw" in html and "from 2 captures" in html

    # The extension's status: its own outcome; in the group view it refreshes the gallery.
    status = auth_client.get(f"/captures/{ext}/status", params={"in": "group"})
    assert status.status_code == 286
    assert "Added 1 new, 1 already known." in status.text
    assert f'id="capture-{cid}-group" hx-swap-oob="true"' in status.text
    assert f'id="capture-{cid}-extend-max" data-tray-max="6"' in status.text
    listing = auth_client.get("/captures").text
    assert "Added 1 new, 1 already known." in listing


def test_add_another_angle_shows_capacity_or_the_refusal(
    auth_client, auth_settings, places, capture, tmp_path
):
    cid = capture["id"]
    html = auth_client.get(f"/captures/{cid}").text
    form = re.search(rf'<form[^>]*id="capture-{cid}-extend-form".*?</form>', html, re.S).group(0)
    assert f'hx-post="/captures/{cid}/extend"' in form and "data-tray" in form
    assert f'id="capture-{cid}-extend-max" data-tray-max="6"' in form
    assert "No photos yet (up to 6)." in form and "data-tray-room" not in form

    # 10 more photos in two extensions: the third shows the group is full.
    for batch in range(2):
        paths = make_jpegs(tmp_path / f"x{batch}", 5, (200 + batch * 7, 150 + batch))
        # distinct images per batch (the colours repeat between batches)
        for index, path in enumerate(paths):
            path.write_bytes(path.read_bytes() + bytes([batch, index]))
        ext = capture_id(post_extend(auth_client, cid, paths))
        assert run(auth_settings, FakeProvider([result([])]))
        assert ext
        if batch == 0:  # room for 5: less than a send, so the tray says it is the room
            html = auth_client.get(f"/captures/{cid}").text
            assert re.search(
                rf'id="capture-{cid}-extend-max" data-tray-max="5" data-tray-room', html
            )
            assert "No photos yet (this capture has room for 5)." in html
    html = auth_client.get(f"/captures/{cid}").text
    assert f'id="capture-{cid}-extend-full"' in html and "extend-form" not in html

    # A failed capture, a reconcile and a text capture cannot get another angle.
    other = capture_id(post_photos(auth_client, places["garage"], make_jpegs(tmp_path / "f", 1)))
    html = auth_client.get(f"/captures/{other}").text
    assert f'id="capture-{other}-extend-refusal"' in html
    assert "Only a finished capture can get another angle." in html
    text = capture_id(post_text(auth_client, "milk", places["garage"]))
    assert "Add another angle" not in auth_client.get(f"/captures/{text}").text
    reconciled = capture_id(
        post_photos(
            auth_client, places["garage"], make_jpegs(tmp_path / "r", 1), url="/captures/reconcile"
        )
    )
    assert run(auth_settings, FakeProvider([result([])]))
    html = auth_client.get(f"/captures/{reconciled}").text
    assert f'id="capture-{reconciled}-group"' in html and "Add another angle" not in html
    assert auth_client.get("/captures/999999").status_code == 404


def test_panel_result_shows_the_gallery_and_the_list_a_link(auth_client, capture):
    cid = capture["id"]
    panel = auth_client.get(f"/captures/{cid}/status")
    assert panel.status_code == 286
    assert f'id="capture-{cid}-gallery"' in panel.text
    assert f'href="/captures/{cid}"' in panel.text and "Add another angle" in panel.text
    assert "hx-swap-oob" not in panel.text
    assert_numeric_styles(panel.text)

    listing = auth_client.get("/captures").text
    assert "data-gallery" not in listing  # no gallery in the list
    link = re.search(rf'<a class="capture-view" id="capture-{cid}-view" href="/captures/{cid}">'
                     r"([^<]*)</a>", listing)  # fmt: skip
    assert link and link.group(1) == "2 photos · View"


def test_item_page_shows_where_it_was_seen(auth_client, auth_settings, capture):
    cid, glue, photos = capture["id"], capture["items"]["Glue"], capture["photos"]
    html = auth_client.get(f"/items/{glue}/edit").text
    assert f'id="item-{glue}-seen"' in html and "Where it was seen" in html
    assert html.count(f'href="/captures/{cid}?item={glue}"') == 2
    assert f'id="item-{glue}-photo-{photos[0]}-photos"' in html
    # Only this item's boxes, highlighted, unlabelled.
    assert set(boxes_by_key(html)) == {"item"}
    assert active_box_keys(html) == ["item", "item"]
    assert "box-label" not in html
    assert "Seen in Garage, 20" in html
    assert_numeric_styles(html)

    screws = capture["items"]["Screws"]  # no box: its photo, unmarked
    html = auth_client.get(f"/items/{screws}/edit").text
    assert "(not marked)" in html and "data-key=" not in html
    with open_db(auth_settings) as conn:
        garage = conn.execute("SELECT id FROM locations WHERE name = 'Garage'").fetchone()[0]
        manual = items.create(conn, items.ItemData(name="Manual", location_id=garage))
    assert "Where it was seen" not in auth_client.get(f"/items/{manual}/edit").text


def test_item_page_of_a_pdf_receipt_item_shows_no_photo(auth_client, auth_settings):
    """A PDF receipt has no display image: its items' page shows no (broken) photo."""
    cid = capture_id(post_receipt(auth_client, FIXTURES / "receipt.pdf"))
    assert run(auth_settings, FakeProvider([result([item("Milk")])]))
    with open_db(auth_settings) as conn:
        (milk,) = capture_items(conn, cid)
        assert milk["photo_id"] is not None
        assert boxes.sightings(conn, milk["id"]) == []
    html = auth_client.get(f"/items/{milk['id']}/edit").text
    assert "Where it was seen" not in html and f'src="/photos/{milk["photo_id"]}"' not in html

    # A photo receipt keeps its unmarked photo.
    cid = capture_id(post_receipt(auth_client))
    assert run(auth_settings, FakeProvider([result([item("Bread")])]))
    with open_db(auth_settings) as conn:
        (bread,) = capture_items(conn, cid)
    html = auth_client.get(f"/items/{bread['id']}/edit").text
    assert "Where it was seen" in html and f'src="/photos/{bread["photo_id"]}"' in html


def test_review_queue_shows_crops_for_boxed_items(auth_client, auth_settings, places, tmp_path):
    cid = capture_id(post_photos(auth_client, places["garage"], make_jpegs(tmp_path / "q", 1)))
    answer = result(
        [
            item("Boxed", confidence=0.3, boxes=[box(0, 200, 200)]),
            item("Plain", confidence=0.3),
        ]
    )
    assert run(auth_settings, FakeProvider([answer]))
    with open_db(auth_settings) as conn:
        ids = {r["name"]: r["id"] for r in capture_items(conn, cid)}
    html = auth_client.get("/review").text
    boxed = re.search(
        rf'<li class="review-row" id="review-item-{ids["Boxed"]}".*?</li>', html, re.S
    )
    crop = re.search(r'<a class="review-crop"[^>]*>', boxed.group(0))
    assert crop and f'href="/captures/{cid}?item={ids["Boxed"]}"' in crop.group(0)
    assert "background-size:" in crop.group(0) and "background-position:" in crop.group(0)
    plain = re.search(
        rf'<li class="review-row" id="review-item-{ids["Plain"]}".*?</li>', html, re.S
    )
    assert "review-crop" not in plain.group(0) and "review-thumb" in plain.group(0)
    assert_numeric_styles(html)


def test_report_shows_each_models_numbered_boxes(make_settings, tmp_path):
    openrouter_catalog.clear_cache()
    settings = make_settings(
        household_password=TEST_PASSWORD, openrouter_catalog_file=CATALOG, ai_model="test/model-a"
    )
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            garage, _ = locations.create(conn, "Garage")
        cid = capture_id(post_photos(client, garage, make_jpegs(tmp_path / "c", 2)))
        assert run(settings, FakeProvider([result([item("Tape"), item("Glue")])]))
        with open_db(settings) as conn:
            case_id = cases.save(conn, cid, [Expected("Tape"), Expected("Glue"), Expected("Rope")])
            photos = cases.get(conn, settings, case_id).photo_ids
            run_id = runs.start(
                conn, ["test/model-a", "test/model-b"], [case_id], 0, datetime.now(UTC)
            )
            answers = {
                "test/model-a": result(
                    [item("Glue", boxes=[box(1)]), item(HOSTILE, boxes=[box(0), box(1, 500)])]
                ),
                "test/model-b": result([item("Tape", boxes=[box(0, 600)])]),
            }

            def provider_for(model: str) -> FakeProvider:
                return FakeProvider([answers[model]], model=model)

            while execution.run_once(conn, settings, provider_for):
                pass
        html = client.get(f"/compare/runs/{run_id}").text
    openrouter_catalog.clear_cache()
    prefix = f"case-{case_id}-gallery"
    assert f'id="{prefix}"' in html
    assert html.count(f'id="{prefix}-photo-') == len(photos) == 2
    switch = re.findall(r'data-gallery-model="(\d)" aria-pressed="(\w+)"', html)
    assert switch == [("0", "true"), ("1", "false")]
    marks = re.findall(
        r'<span class="box" data-key="(\d)-(\d+)" data-model="(\d)"( hidden)?[^>]*>'
        r'<span class="box-label[^"]*">(\d+)</span>',
        html,
    )
    by_model = {}
    for model, number, model_attr, hidden, label in marks:
        assert model == model_attr and number == label
        assert bool(hidden) == (model != "0")  # only the first model's boxes show at first
        by_model.setdefault(model, []).append(int(label))
    assert sorted(by_model["0"]) == [1, 2, 2] and by_model["1"] == [1]
    # Item lines carry the same numbers; missed lines have none.
    assert re.search(r'<span class="line-num" aria-label="box 2">2</span>\s*Tape&#34;', html)
    missed = re.findall(r'<li class="line-missed">(.*?)</li>', html, re.S)
    assert missed and all("line-num" not in line for line in missed)
    assert HOSTILE not in html
    assert_numeric_styles(html.split("<h2>Cases</h2>", 1)[1])


def test_group_page_of_an_unfinished_root_refreshes_when_it_finishes(
    auth_client, auth_settings, places, tmp_path
):
    cid = capture_id(post_photos(auth_client, places["garage"], make_jpegs(tmp_path / "u", 2)))
    html = auth_client.get(f"/captures/{cid}").text
    assert f'hx-get="/captures/{cid}/status?in=group"' in html
    assert "hx-swap-oob" not in html  # out-of-band parts only in the endpoint's answers
    assert f'id="capture-{cid}-extend-refusal"' in html
    assert run(auth_settings, FakeProvider([result([item("Tape", boxes=[box(0)])])]))

    status = auth_client.get(f"/captures/{cid}/status", params={"in": "group"}).text
    group = re.search(rf'<section class="group-photos" id="capture-{cid}-group"[^>]*>', status)
    assert group and 'hx-swap-oob="true"' in group.group(0)
    assert status.count(f'id="capture-{cid}-gallery-photo-') == 2
    meta = re.search(rf'<p class="muted" id="capture-{cid}-meta"[^>]*>(.*?)</p>', status, re.S)
    assert meta and 'hx-swap-oob="true"' in meta.group(0) and "2 photos" in meta.group(1)
    extend = re.search(rf'<section class="panel extend" id="capture-{cid}-extend"[^>]*>', status)
    assert extend and 'hx-swap-oob="true"' in extend.group(0)
    assert f'id="capture-{cid}-extend-form"' in status
    # The summary is the live region; the gallery is outside it.
    summary = re.search(r'<div class="capture-summary" role="status">(.*?)</div>', status, re.S)
    assert summary and "data-gallery" not in summary.group(1)

    # An extension's answer refreshes the meta line and the tray limit, not the section.
    ext = capture_id(post_extend(auth_client, cid, make_jpegs(tmp_path / "e", 5, (300, 200))))
    assert run(auth_settings, FakeProvider([result([])]))
    status = auth_client.get(f"/captures/{ext}/status", params={"in": "group"}).text
    assert re.search(rf'id="capture-{cid}-meta" hx-swap-oob="true">[^<]*7 photos', status)
    limit = re.search(rf'<span id="capture-{cid}-extend-max"[^>]*>', status).group(0)
    assert 'data-tray-max="5"' in limit and "data-tray-room" in limit and "hx-swap-oob" in limit
    assert f'id="capture-{cid}-extend"' not in status

"""Location photo, item thumbnails, fixed outlines, inline rename, Delete and the collapsible
Tag section (spec-location-photo, Revision 1): captures record their overview photo, the
location page shows it (pinned or automatic) with its direct items' outlines, item rows show
a crop from one query, the item photo view saves user outlines that later captures keep,
rename and delete work in place, and every new route is protected."""

import re
import sqlite3
from contextlib import contextmanager

import pytest

import app.db.deps
from app.ai.fake import FakeProvider
from app.capture import boxes, groups
from app.capture import overview as overview_mod
from app.compare import cases
from app.db.connection import connect, write_tx
from app.inventory import items, locations, search
from tests.capture_helpers import (
    capture_id,
    capture_items,
    capture_row,
    item,
    make_jpegs,
    post_extend,
    post_photos,
    result,
    run,
)
from tests.conftest import logged_in
from tests.integration.test_auth import PROTECTED
from tests.integration.test_capture_gallery_ui import assert_numeric_styles
from tests.inventory_helpers import HX, open_db, seed_items


def box(image: int, top: int = 100, left: int = 100) -> dict[str, object]:
    return {"image": image, "box_2d": [top, left, top + 200, left + 300]}


def photo_ids(conn, cid: int) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM photos WHERE capture_id = ? ORDER BY position, id", (cid,)
        )
    ]


def do_capture(client, settings, location_id, folder, answer, count=2) -> int:
    cid = capture_id(post_photos(client, location_id, make_jpegs(folder, count)))
    assert run(settings, FakeProvider([answer]))
    with open_db(settings) as conn:
        assert capture_row(conn, cid)["status"] == "done", capture_row(conn, cid)["error"]
    return cid


def add_angle(client, root: int, tmp_path) -> int:
    """Queue an "another angle" capture of ``root`` with one new photo; its id."""
    return capture_id(post_extend(client, root, make_jpegs(tmp_path / "e", 1, (330, 250))))


def overview_keys(html: str) -> list[str]:
    return re.findall(r'data-overview-key="(\d+)"', html)


@pytest.fixture
def garage(auth_client, auth_settings, tmp_path) -> dict:
    """Garage with a done 2-photo capture: Tape (photo 1), Glue (photos 1, 2), Rake (photo 2),
    Screws (no box); the model names photo 2 as the overview."""
    with open_db(auth_settings) as conn:
        garage_id, code = locations.create(conn, "Garage")
    answer = result(
        [
            item("Tape", boxes=[box(0)]),
            item("Glue", boxes=[box(0, 500, 500), box(1, 100, 100)]),
            item("Rake", boxes=[box(1, 600, 600)]),
            item("Screws"),
        ]
    )
    answer.overview_image = 1
    cid = do_capture(auth_client, auth_settings, garage_id, tmp_path / "g", answer)
    with open_db(auth_settings) as conn:
        ids = {r["name"]: r["id"] for r in capture_items(conn, cid)}
        photos = photo_ids(conn, cid)
    return {"id": garage_id, "code": code, "capture": cid, "items": ids, "photos": photos}


# ---------------------------------------------------------------- overview photo (R1, R2, R8)


def test_capture_records_the_model_overview_photo(auth_settings, garage):
    with open_db(auth_settings) as conn:
        assert capture_row(conn, garage["capture"])["overview_photo_id"] == garage["photos"][1]
        location = locations.get(conn, garage["id"])
        assert location["overview_photo_id"] is None  # apply never writes locations
        view = overview_mod.overview(conn, location)
    assert view is not None and not view.pinned
    assert (view.photo_id, view.root_capture_id) == (garage["photos"][1], garage["capture"])
    assert (view.width, view.height) == (320, 240)


@pytest.mark.parametrize("overview", [None, 7, -1, "1", True])
def test_invalid_overview_image_never_fails_and_falls_back(
    auth_client, auth_settings, tmp_path, overview
):
    with open_db(auth_settings) as conn:
        shed, _ = locations.create(conn, "Shed")
    found = [item("A", boxes=[box(0)]), item("B", boxes=[box(1)]), item("C", boxes=[box(1)])]
    answer = result(found).model_copy(update={"overview_image": overview})
    cid = do_capture(auth_client, auth_settings, shed, tmp_path / "s", answer, count=3)
    with open_db(auth_settings) as conn:
        assert capture_row(conn, cid)["overview_photo_id"] == photo_ids(conn, cid)[1]


def test_location_page_shows_the_photo_with_direct_item_outlines(
    auth_client, auth_settings, garage
):
    ids = garage["items"]
    with open_db(auth_settings) as conn:
        shelf, _ = locations.create(conn, "Shelf", garage["id"])
        items.move_item(conn, ids["Rake"], shelf)  # no longer directly here
    html = auth_client.get(f"/l/{garage['code']}").text
    section = html.split('id="location-photo"', 1)[1].split("</section>", 1)[0]
    assert f'src="/photos/{garage["photos"][1]}"' in section
    assert sorted(overview_keys(section)) == sorted([str(ids["Glue"])])
    assert "data-gallery" not in section and "box-label" not in section
    assert f'href="/captures/{garage["capture"]}" id="location-photo-all">All photos' in section
    assert "Pick automatically" not in section
    assert "aspect-ratio:320/240" in section
    assert_numeric_styles(section)
    # The photo comes right after the title, before the capture actions.
    assert html.index('id="location-photo"') < html.index('class="actions"')
    # Subtree mode: still only the direct items' outlines, and it says so.
    sub = auth_client.get(f"/l/{garage['code']}?sub=1").text
    assert "Outlines show items directly here." in sub
    assert overview_keys(sub) == [str(ids["Glue"])]


def test_used_up_and_deleted_items_have_no_outline(auth_client, auth_settings, garage, tmp_path):
    ids = garage["items"]
    with open_db(auth_settings) as conn:
        items.use_up(conn, ids["Glue"])
        items.delete_item(conn, ids["Rake"])
    html = auth_client.get(f"/l/{garage['code']}").text
    assert 'id="location-photo"' in html and overview_keys(html) == []


def test_no_photo_without_a_capture_and_none_for_the_inbox(auth_client, auth_settings, tmp_path):
    with open_db(auth_settings) as conn:
        _empty, code = locations.create(conn, "Empty")
        inbox = locations.inbox(conn)
        doomed, _ = locations.create(conn, "Doomed")
    assert 'id="location-photo"' not in auth_client.get(f"/l/{code}").text
    # A capture of the Inbox itself, and one of a location deleted before it ran (its items
    # land in the Inbox): neither gives the Inbox a photo.
    do_capture(auth_client, auth_settings, inbox["id"], tmp_path / "i", result([item("A")]), 1)
    cid = capture_id(post_photos(auth_client, doomed, make_jpegs(tmp_path / "d", 1)))
    with open_db(auth_settings) as conn:
        locations.delete(conn, doomed)
    assert run(auth_settings, FakeProvider([result([item("B", boxes=[box(0)])])]))
    with open_db(auth_settings) as conn:
        assert capture_row(conn, cid)["status"] == "done"
        assert overview_mod.overview(conn, locations.inbox(conn)) is None
        inbox_code = inbox["short_code"]
    assert 'id="location-photo"' not in auth_client.get(f"/l/{inbox_code}").text


def test_old_captures_fall_back_to_the_photo_with_most_live_boxes(auth_settings, garage):
    with open_db(auth_settings) as conn:
        with write_tx(conn):
            conn.execute("UPDATE captures SET overview_photo_id = NULL")
        view = overview_mod.overview(conn, locations.get(conn, garage["id"]))
        # Tape + Glue on photo 1, Glue + Rake on photo 2: a tie takes the first photo.
        assert view.photo_id == garage["photos"][0]
        items.use_up(conn, garage["items"]["Tape"])
        view = overview_mod.overview(conn, locations.get(conn, garage["id"]))
        assert view.photo_id == garage["photos"][1]  # boxes of live items only
        items.delete_item(conn, garage["items"]["Glue"])
        view = overview_mod.overview(conn, locations.get(conn, garage["id"]))
        assert view.photo_id == garage["photos"][1]
        items.use_up(conn, garage["items"]["Rake"])
        view = overview_mod.overview(conn, locations.get(conn, garage["id"]))
        assert view.photo_id == garage["photos"][0]  # no boxes: the first photo


def test_latest_capture_wins_and_extension_keeps_one_per_group(
    auth_client, auth_settings, garage, tmp_path
):
    # An extension re-sends the group's photos; the model may pick an old one.
    ext = add_angle(auth_client, garage["capture"], tmp_path)
    answer = result([item("Hose", boxes=[box(2)])]).model_copy(update={"overview_image": 0})
    assert run(auth_settings, FakeProvider([answer]))
    with open_db(auth_settings) as conn:
        assert capture_row(conn, ext)["overview_photo_id"] == garage["photos"][0]
        view = overview_mod.overview(conn, locations.get(conn, garage["id"]))
        assert view.photo_id == garage["photos"][0]
        assert view.root_capture_id == garage["capture"]
        # The root keeps its own choice; the group's latest capture decides.
        assert capture_row(conn, garage["capture"])["overview_photo_id"] == garage["photos"][1]
        assert len(groups.done_photo_ids(conn, garage["capture"])) == 3
    # A later reconcile capture of the location becomes the photo.
    cid = capture_id(
        post_photos(
            auth_client, garage["id"], make_jpegs(tmp_path / "r", 2), url="/captures/reconcile"
        )
    )
    assert run(auth_settings, FakeProvider([result([]).model_copy(update={"overview_image": 1})]))
    with open_db(auth_settings) as conn:
        view = overview_mod.overview(conn, locations.get(conn, garage["id"]))
        assert view.photo_id == photo_ids(conn, cid)[1]


# ---------------------------------------------------------------- pin / unpin (R11)


def test_pin_and_unpin(auth_client, auth_settings, garage, tmp_path):
    first = garage["photos"][0]
    url = f"/l/{garage['code']}/overview"
    assert auth_client.post(url, data={"photo_id": str(first)}).status_code == 403  # HX only
    pinned = auth_client.post(url, data={"photo_id": str(first)}, headers=HX)
    assert pinned.status_code == 200 and "Location photo" in pinned.text
    page = auth_client.get(f"/l/{garage['code']}").text
    assert f'src="/photos/{first}"' in page.split('id="location-photo"', 1)[1]
    assert 'id="location-photo-auto"' in page
    # A later capture does not replace a pinned photo.
    do_capture(auth_client, auth_settings, garage["id"], tmp_path / "later", result([]), 1)
    with open_db(auth_settings) as conn:
        assert overview_mod.overview(conn, locations.get(conn, garage["id"])).photo_id == first
    auto = auth_client.post(f"{url}/auto", headers=HX)
    assert auto.status_code == 200 and auto.headers["hx-refresh"] == "true"
    with open_db(auth_settings) as conn:
        view = overview_mod.overview(conn, locations.get(conn, garage["id"]))
        assert view.photo_id != first and not view.pinned


def test_pin_refuses_photos_of_other_places(auth_client, auth_settings, garage, tmp_path):
    with open_db(auth_settings) as conn:
        shed, _ = locations.create(conn, "Shed")
    other = do_capture(auth_client, auth_settings, shed, tmp_path / "s", result([]), 1)
    with open_db(auth_settings) as conn:
        (shed_photo,) = photo_ids(conn, other)
        inbox_code = locations.inbox(conn)["short_code"]
    url = f"/l/{garage['code']}/overview"
    for photo in (str(shed_photo), "999999", "", "x"):
        refused = auth_client.post(url, data={"photo_id": photo}, headers=HX)
        assert refused.status_code == 400, photo
    assert (
        "not one of this location"
        in auth_client.post(url, data={"photo_id": str(shed_photo)}, headers=HX).text
    )
    inbox = auth_client.post(
        f"/l/{inbox_code}/overview", data={"photo_id": str(shed_photo)}, headers=HX
    )
    assert inbox.status_code == 400
    with open_db(auth_settings) as conn:
        assert locations.get(conn, garage["id"])["overview_photo_id"] is None


def test_pinned_photo_without_a_size_falls_back(auth_settings, garage):
    with open_db(auth_settings) as conn:
        locations.pin_overview(conn, garage["id"], garage["photos"][0])
        with write_tx(conn):
            conn.execute("UPDATE photos SET width = NULL WHERE id = ?", (garage["photos"][0],))
        view = overview_mod.overview(conn, locations.get(conn, garage["id"]))
    assert view.photo_id == garage["photos"][1] and not view.pinned


def test_capture_page_offers_use_as_location_photo(auth_client, auth_settings, garage, tmp_path):
    page = auth_client.get(f"/captures/{garage['capture']}").text
    for photo in garage["photos"]:
        assert f'id="capture-{garage["capture"]}-gallery-pin-{photo}"' in page
    assert f'hx-post="/l/{garage["code"]}/overview"' in page
    # Not for the Inbox, nor for a capture whose location was deleted.
    with open_db(auth_settings) as conn:
        inbox = locations.inbox(conn)["id"]
        gone, _ = locations.create(conn, "Gone")
    inbox_capture = do_capture(auth_client, auth_settings, inbox, tmp_path / "i", result([]), 1)
    gone_capture = do_capture(auth_client, auth_settings, gone, tmp_path / "x", result([]), 1)
    with open_db(auth_settings) as conn:
        locations.delete(conn, gone)
    for cid in (inbox_capture, gone_capture):
        assert "Use as location photo" not in auth_client.get(f"/captures/{cid}").text


# ---------------------------------------------------------------- thumbnails (R3, R4)


@contextmanager
def traced(monkeypatch):
    """Every SQL statement the app's request connections run."""
    statements: list[str] = []

    def tracing(path):
        conn = connect(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(app.db.deps, "connect", tracing)
    yield statements
    monkeypatch.undo()


def thumb_ids(html: str) -> list[int]:
    return [int(i) for i in re.findall(r'class="item-thumb" id="thumb-(\d+)"', html)]


@pytest.mark.parametrize("where", ["location", "search_page", "search_partial"])
def test_rows_show_thumbnails_only_for_boxed_items(auth_client, garage, where):
    ids = garage["items"]
    if where == "location":
        html = auth_client.get(f"/l/{garage['code']}").text
    else:
        headers = HX if where == "search_partial" else {}
        html = auth_client.get(f"/search?loc={garage['id']}", headers=headers).text
    assert sorted(thumb_ids(html)) == sorted([ids["Tape"], ids["Glue"], ids["Rake"]])
    assert f'href="/items/{ids["Tape"]}/photo"' in html
    assert ids["Screws"] not in thumb_ids(html)
    assert_numeric_styles(html.split('id="location-items"', 1)[-1])


def test_thumbnails_take_one_query_for_all_rows(auth_client, auth_settings, garage, monkeypatch):
    with traced(monkeypatch) as few:
        auth_client.get(f"/search?loc={garage['id']}")
    with open_db(auth_settings) as conn:
        seed_items(
            conn, [items.ItemData(name=f"Extra {n}", location_id=garage["id"]) for n in range(6)]
        )
        extra = conn.execute("SELECT id FROM items WHERE name LIKE 'Extra%'").fetchall()
        with write_tx(conn):
            for row in extra:
                conn.execute(
                    "INSERT INTO item_boxes (item_id, photo_id, capture_id, idx, x0, y0, x1, y1)"
                    " VALUES (?, ?, ?, 0, 0.1, 0.1, 0.3, 0.3)",
                    (row[0], garage["photos"][0], garage["capture"]),
                )
    with traced(monkeypatch) as many:
        html = auth_client.get(f"/search?loc={garage['id']}").text
    assert len(thumb_ids(html)) == 9
    box_reads = [s for s in many if "item_boxes" in s]
    assert len(box_reads) == 1
    assert len(box_reads) == len([s for s in few if "item_boxes" in s])


def test_quick_actions_keep_the_thumbnail(auth_client, auth_settings, garage):
    tape = garage["items"]["Tape"]
    thumb = f'id="thumb-{tape}"'
    assert (
        thumb in auth_client.post(f"/items/{tape}/quantity", data={"delta": "1"}, headers=HX).text
    )
    auth_client.post(f"/items/{tape}/used-up", headers=HX)
    assert thumb in auth_client.post(f"/items/{tape}/restore", headers=HX).text
    with open_db(auth_settings) as conn:
        shed, _ = locations.create(conn, "Shed")
    auth_client.post(f"/items/{tape}/move", data={"location_id": str(shed)}, headers=HX)
    undo = auth_client.post(
        f"/items/{tape}/move", data={"location_id": str(garage["id"]), "undo": "1"}, headers=HX
    )
    assert thumb in undo.text
    assert thumb in auth_client.post(f"/items/{tape}/name", data={"name": "Tape"}, headers=HX).text
    assert thumb in auth_client.get(f"/items/{tape}/row").text


def test_preferred_box_is_the_user_box_then_the_newest_capture(
    auth_client, auth_settings, garage, tmp_path
):
    glue = garage["items"]["Glue"]
    with open_db(auth_settings) as conn:
        assert boxes.review_crops(conn, [glue])[glue].photo_id == garage["photos"][0]
    # A newer capture boxes it on its own photo: that becomes the thumbnail.
    ext = add_angle(auth_client, garage["capture"], tmp_path)
    answer = result([item("Glue", matched_existing_id=glue, boxes=[box(2, 300, 300)])])
    assert run(auth_settings, FakeProvider([answer]))
    with open_db(auth_settings) as conn:
        (new_photo,) = photo_ids(conn, ext)
        assert boxes.review_crops(conn, [glue])[glue].photo_id == new_photo
        # A user box wins over any model box, wherever it is.
        boxes.set_user_box(conn, glue, garage["photos"][1], (0.5, 0.5, 0.9, 0.9))
        crop = boxes.review_crops(conn, [glue])[glue]
        assert crop.photo_id == garage["photos"][1]
        assert crop.x0 <= 0.5 and crop.x1 >= 0.9


# ---------------------------------------------------------------- item photo view (R5, R7)


def test_photo_view_highlights_the_item_and_switches_photos(auth_client, garage):
    glue = garage["items"]["Glue"]
    first, second = garage["photos"]
    page = auth_client.get(f"/items/{glue}/photo")
    assert page.status_code == 200
    html = page.text
    main = html.split('id="item-photo"', 1)[1].split('id="item-photo-choices"', 1)[0]
    assert f'src="/photos/{first}"' in main and 'class="box is-active"' in main
    assert 'data-draw data-min="0.02"' in html and f'hx-post="/items/{glue}/box"' in html
    assert f'name="photo_id" value="{first}"' in html
    choices = re.findall(r'id="photo-choice-(\d+)" href', html)
    assert choices == [str(first), str(second)]
    other = auth_client.get(f"/items/{glue}/photo?photo={second}").text
    assert f'name="photo_id" value="{second}"' in other
    assert 'aria-current="true"' in other.split(f'id="photo-choice-{second}"', 1)[1][:200]
    # An unknown photo falls back to the default.
    fallback = auth_client.get(f"/items/{glue}/photo?photo=999999").text
    assert f'name="photo_id" value="{first}"' in fallback
    assert "Remove outline" not in html
    assert_numeric_styles(html)


def test_photo_view_offers_the_location_photo_and_404s_without_photos(
    auth_client, auth_settings, garage
):
    tape, screws = garage["items"]["Tape"], garage["items"]["Screws"]
    # Tape was only boxed on photo 1; the location's photo (photo 2) is offered too.
    html = auth_client.get(f"/items/{tape}/photo").text
    assert re.findall(r'id="photo-choice-(\d+)" href', html) == [
        str(garage["photos"][0]),
        str(garage["photos"][1]),
    ]
    with open_db(auth_settings) as conn:
        manual = seed_items(conn, [items.ItemData(name="Manual", location_id=garage["id"])])[0]
        empty, _ = locations.create(conn, "Empty")
        lonely = seed_items(conn, [items.ItemData(name="Lonely", location_id=empty)])[0]
    # Screws has no box but its own photo; Manual is offered the location's photo.
    assert auth_client.get(f"/items/{screws}/photo").status_code == 200
    assert auth_client.get(f"/items/{manual}/photo").status_code == 200
    assert auth_client.get(f"/items/{lonely}/photo").status_code == 404
    assert auth_client.get("/items/999999/photo").status_code == 404


def post_box(client, item_id, photo_id, coords=("0.1", "0.2", "0.4", "0.6"), headers=HX):
    data = dict(zip(("x0", "y0", "x1", "y1"), coords, strict=True)) | {"photo_id": str(photo_id)}
    return client.post(f"/items/{item_id}/box", data=data, headers=headers)


def user_rows(conn, item_id):
    return conn.execute(
        "SELECT photo_id, capture_id, idx, x0, y0, x1, y1, source FROM item_boxes"
        " WHERE item_id = ? ORDER BY photo_id, idx",
        (item_id,),
    ).fetchall()


def test_saving_an_outline_replaces_boxes_on_that_photo_only(auth_client, auth_settings, garage):
    glue = garage["items"]["Glue"]
    first, second = garage["photos"]
    saved = post_box(auth_client, glue, first)
    assert saved.status_code == 200
    assert saved.headers["hx-redirect"] == f"/items/{glue}/photo?photo={first}&saved=1"
    with open_db(auth_settings) as conn:
        rows = [tuple(r) for r in user_rows(conn, glue)]
        crop = boxes.review_crops(conn, [glue])[glue]
    assert rows[0] == (first, garage["capture"], 0, 0.1, 0.2, 0.4, 0.6, "user")
    assert rows[1][0] == second and rows[1][-1] == "model"  # the other photo is untouched
    assert len(rows) == 2 and crop.photo_id == first
    assert "Outline saved." in auth_client.get(saved.headers["hx-redirect"]).text
    # The row thumbnail shows the new crop.
    row = auth_client.get(f"/items/{glue}/row").text
    expected = re.search(r'background-size:([^;"]+)', row).group(1)
    assert expected == crop.background_size


@pytest.mark.parametrize(
    "coords",
    [
        ("nan", "0.1", "0.5", "0.5"),
        ("0.1", "inf", "0.5", "0.5"),
        ("0.5", "0.1", "0.1", "0.5"),
        ("0.1", "0.1", "0.11", "0.5"),
        ("-0.1", "0.1", "0.5", "0.5"),
        ("0.1", "0.1", "0.5", "1.5"),
        ("a", "b", "c", "d"),
    ],
)
def test_invalid_outlines_are_rejected(auth_client, auth_settings, garage, coords):
    glue = garage["items"]["Glue"]
    with open_db(auth_settings) as conn:
        before = [tuple(r) for r in user_rows(conn, glue)]
    refused = post_box(auth_client, glue, garage["photos"][0], coords)
    assert refused.status_code == 400 and 'role="alert"' in refused.text
    with open_db(auth_settings) as conn:
        assert [tuple(r) for r in user_rows(conn, glue)] == before


def test_outline_photo_must_be_one_of_the_choices(auth_client, auth_settings, garage, tmp_path):
    glue = garage["items"]["Glue"]
    with open_db(auth_settings) as conn:
        shed, _ = locations.create(conn, "Shed")
    other = do_capture(auth_client, auth_settings, shed, tmp_path / "s", result([]), 1)
    with open_db(auth_settings) as conn:
        (foreign,) = photo_ids(conn, other)
    for photo in (foreign, 999999, ""):
        assert post_box(auth_client, glue, photo).status_code == 400
    # A photo without a display size is neither offered nor accepted.
    with open_db(auth_settings) as conn:
        with write_tx(conn):
            conn.execute(
                "UPDATE photos SET width = NULL, height = NULL WHERE id = ?",
                (garage["photos"][1],),
            )
        assert [c.photo_id for c in boxes.photo_choices(conn, glue)] == [garage["photos"][0]]
    assert post_box(auth_client, glue, garage["photos"][1]).status_code == 400
    assert (
        f'id="photo-choice-{garage["photos"][1]}"'
        not in auth_client.get(f"/items/{glue}/photo").text
    )


def test_outline_post_needs_htmx_origin_and_a_live_item(auth_client, auth_settings, garage):
    glue = garage["items"]["Glue"]
    photo = garage["photos"][0]
    assert post_box(auth_client, glue, photo, headers={}).status_code == 403
    no_origin = auth_client.post(
        f"/items/{glue}/box",
        data={"photo_id": str(photo), "x0": "0.1", "y0": "0.1", "x1": "0.5", "y1": "0.5"},
        headers=HX | {"Origin": "http://evil.example"},
    )
    assert no_origin.status_code == 403
    with open_db(auth_settings) as conn:
        items.use_up(conn, glue)
    assert post_box(auth_client, glue, photo).status_code == 404


def test_later_captures_never_overwrite_a_user_box(auth_client, auth_settings, garage, tmp_path):
    glue = garage["items"]["Glue"]
    first = garage["photos"][0]
    assert post_box(auth_client, glue, first).status_code == 200
    # The extension re-sends photo 1 and boxes Glue there again (and on its new photo).
    ext = add_angle(auth_client, garage["capture"], tmp_path)
    answer = result(
        [item("Glue", matched_existing_id=glue, boxes=[box(0, 700, 700), box(2, 300, 300)])]
    )
    assert run(auth_settings, FakeProvider([answer]))
    with open_db(auth_settings) as conn:
        rows = user_rows(conn, glue)
        on_first = [tuple(r) for r in rows if r["photo_id"] == first]
        (new_photo,) = photo_ids(conn, ext)
        assert on_first == [(first, garage["capture"], 0, 0.1, 0.2, 0.4, 0.6, "user")]
        assert any(r["photo_id"] == new_photo and r["source"] == "model" for r in rows)
        # Directly: replace_boxes skips photos with a user box and never deletes user rows.
        with write_tx(conn):
            boxes.replace_boxes(conn, glue, ext, boxes.photo_boxes(answer.items[0], [first]))
        assert [tuple(r) for r in user_rows(conn, glue) if r["photo_id"] == first] == on_first


def test_extension_outcome_fallback_ignores_user_boxes(
    auth_client, auth_settings, garage, tmp_path
):
    ext = add_angle(auth_client, garage["capture"], tmp_path)
    assert run(auth_settings, FakeProvider([result([])]))
    with open_db(auth_settings) as conn:
        (new_photo,) = photo_ids(conn, ext)
        with write_tx(conn):
            conn.execute("UPDATE captures SET known_count = NULL WHERE id = ?", (ext,))
        boxes.set_user_box(conn, garage["items"]["Tape"], new_photo, (0.1, 0.1, 0.5, 0.5))
        assert groups.extension_outcome(conn, ext) == (0, 0)


# ---------------------------------------------------------------- inline rename (R9)


def test_inline_rename(auth_client, auth_settings, garage):
    tape = garage["items"]["Tape"]
    with open_db(auth_settings) as conn, write_tx(conn):
        conn.execute("UPDATE items SET needs_review = 1 WHERE id = ?", (tape,))
    row = auth_client.get(f"/l/{garage['code']}").text
    assert re.search(rf'<button[^>]*class="item-name"[^>]*hx-get="/items/{tape}/name"', row, re.S)
    edit = auth_client.get(f"/items/{tape}/name").text
    assert f'id="rename-{tape}-input" value="Tape"' in edit and "data-rename-input" in edit
    assert f'id="more-{tape}"' not in edit  # the quick actions make room for the input
    assert f'hx-get="/items/{tape}/row"' in edit
    saved = auth_client.post(f"/items/{tape}/name", data={"name": "  Duct   tape "}, headers=HX)
    assert saved.status_code == 200 and ">Duct tape</button>" in saved.text
    with open_db(auth_settings) as conn:
        row = items.get(conn, tape)
        assert row["name"] == "Duct tape" and row["needs_review"] == 1
        assert [r["id"] for r in search.search(conn, "duct")] == [tape]
        assert search.search(conn, "tape")[0]["name"] == "Duct tape"


@pytest.mark.parametrize("name", ["", "   ", "x" * 201])
def test_bad_names_answer_400_inline(auth_client, auth_settings, garage, name):
    tape = garage["items"]["Tape"]
    refused = auth_client.post(f"/items/{tape}/name", data={"name": name}, headers=HX)
    assert refused.status_code == 400 and 'role="alert"' in refused.text
    assert "Name is" in refused.text
    with open_db(auth_settings) as conn:
        assert items.get(conn, tape)["name"] == "Tape"


def test_rename_needs_htmx_and_a_live_item(auth_client, auth_settings, garage):
    tape = garage["items"]["Tape"]
    assert auth_client.post(f"/items/{tape}/name", data={"name": "X"}).status_code == 403
    with open_db(auth_settings) as conn:
        items.use_up(conn, tape)
    assert (
        auth_client.post(f"/items/{tape}/name", data={"name": "X"}, headers=HX).status_code == 404
    )
    assert auth_client.get(f"/items/{tape}/name").status_code == 404


def test_edit_form_shares_the_name_rule():
    data, _errors = items.parse_form({"name": "  a  b ", "location_id": "1"})
    assert data is not None and data.name == "a b"
    assert items.parse_form({"name": " ", "location_id": "1"})[1]["name"] == "Name is required."
    assert items.parse_form({"name": "y" * 201, "location_id": "1"})[1]["name"] == (
        "Name is too long."
    )


# ---------------------------------------------------------------- delete (R10)


def test_sheet_offers_delete_behind_a_confirm(auth_client, garage):
    tape = garage["items"]["Tape"]
    sheet = auth_client.get(f"/items/{tape}/actions").text
    ask = sheet.index('id="sheet-delete-ask"')
    assert sheet.index('id="sheet-used-up"') < ask
    confirm = re.search(r'<button[^>]*id="sheet-delete-confirm"[^>]*>', sheet).group(0)
    assert "data-sheet-action" in confirm and f'hx-post="/items/{tape}/delete"' in confirm
    assert "Use this for wrong detections." in sheet
    assert "data-sheet-action" not in sheet[ask - 200 : ask + 60]


def test_delete_and_undo(auth_client, auth_settings, garage):
    tape = garage["items"]["Tape"]
    assert auth_client.post(f"/items/{tape}/delete").status_code == 403
    deleted = auth_client.post(f"/items/{tape}/delete", headers=HX)
    assert deleted.status_code == 200 and "Deleted: Tape" in deleted.text
    assert f'hx-post="/items/{tape}/restore"' in deleted.text
    with open_db(auth_settings) as conn:
        row = items.get(conn, tape, include_deleted=True)
        assert row["deleted_at"] and row["deleted_reason"] == "deleted"
        assert search.search(conn, "tape") == []
        assert conn.execute(
            "SELECT count(*) FROM item_boxes WHERE item_id = ?", (tape,)
        ).fetchone()[0]
    assert auth_client.post(f"/items/{tape}/delete", headers=HX).status_code == 404
    export = auth_client.get("/api/export.json").json()
    assert {i["id"]: i["deleted_reason"] for i in export["items"]}[tape] == "deleted"
    restored = auth_client.post(f"/items/{tape}/restore", headers=HX)
    assert restored.status_code == 200 and f'id="more-{tape}"' in restored.text
    with open_db(auth_settings) as conn:
        row = items.get(conn, tape)
        assert row["deleted_at"] is None and row["deleted_reason"] is None
        assert [r["id"] for r in search.search(conn, "tape")] == [tape]


def test_used_up_records_its_reason(auth_client, auth_settings, garage):
    tape = garage["items"]["Tape"]
    used = auth_client.post(f"/items/{tape}/used-up", headers=HX)
    assert "Used up: Tape" in used.text
    with open_db(auth_settings) as conn:
        assert items.get(conn, tape, include_deleted=True)["deleted_reason"] == "used_up"


def test_review_queue_delete_is_a_delete(auth_client, auth_settings, garage):
    tape, glue = garage["items"]["Tape"], garage["items"]["Glue"]
    with open_db(auth_settings) as conn, write_tx(conn):
        conn.execute("UPDATE items SET needs_review = 1 WHERE id IN (?, ?)", (tape, glue))
    assert auth_client.post(f"/review/items/{tape}/delete", headers=HX).status_code == 200
    with open_db(auth_settings) as conn:
        assert items.get(conn, tape, include_deleted=True)["deleted_reason"] == "deleted"
    # Deleting from a row leaves the review queue's other entries alone.
    auth_client.post(f"/items/{tape}/restore", headers=HX)
    auth_client.post(f"/items/{tape}/delete", headers=HX)
    review = auth_client.get("/review").text
    assert f'id="review-item-{glue}"' in review and f'id="review-item-{tape}"' not in review
    assert f'id="review-crop-{glue}"' in review


def test_deleted_items_leave_prefills_and_galleries(auth_client, auth_settings, garage):
    ids = garage["items"]
    with open_db(auth_settings) as conn:
        items.delete_item(conn, ids["Rake"])
        items.use_up(conn, ids["Tape"])
        names = [e.name for e in cases.prefill(conn, garage["capture"]).items]
        group = groups.group(conn, garage["capture"])
    assert "Rake" not in names and "Tape" in names and "Glue" in names
    assert {e.name: e.state for e in group.items} == {
        "Tape": "used_up",
        "Glue": "here",
        "Screws": "here",
    }
    page = auth_client.get(f"/captures/{garage['capture']}").text
    assert "Rake" not in page and "used up" in page


def test_deleted_items_are_still_sent_as_gone(auth_client, auth_settings, garage, tmp_path):
    rake = garage["items"]["Rake"]
    with open_db(auth_settings) as conn:
        items.delete_item(conn, rake)
    add_angle(auth_client, garage["capture"], tmp_path)
    fake = FakeProvider([result([])])
    assert run(auth_settings, fake)
    gone = [e for e in fake.requests[0].existing_items if e.gone]
    assert [e.id for e in gone] == [rake]


# ---------------------------------------------------------------- Tag section (G), routes


def test_tag_section_is_a_closed_details(auth_client, garage):
    html = auth_client.get(f"/l/{garage['code']}").text
    match = re.search(r'<details class="panel" id="tag">(.*?)</details>', html, re.S)
    assert match, "Tag section"
    body = match.group(1)
    assert "<summary>Tag: QR code, link and labels</summary>" in body
    for needle in ('id="permanent-url"', 'class="qr"', "qr.png?scale=10", 'id="label-sheet"'):
        assert needle in body


def test_new_routes_are_protected(auth_settings):
    for route in [
        ("GET", "/items/1/photo"),
        ("POST", "/items/1/box"),
        ("GET", "/items/1/name"),
        ("POST", "/items/1/name"),
        ("GET", "/items/1/row"),
        ("POST", "/items/1/delete"),
        ("POST", "/l/1/overview"),
        ("POST", "/l/1/overview/auto"),
    ]:
        assert route in PROTECTED
    with logged_in(auth_settings) as client:
        assert client.get("/items/1/photo", follow_redirects=False).status_code == 404


# ---------------------------------------------------------------- review follow-ups


def test_automatic_photo_must_be_displayable(auth_settings, garage):
    first, second = garage["photos"]
    with open_db(auth_settings) as conn:
        location = locations.get(conn, garage["id"])
        assert overview_mod.overview(conn, location).photo_id == second  # the model's choice
        with write_tx(conn):
            conn.execute("UPDATE photos SET display_rel_path = NULL WHERE id = ?", (second,))
        assert overview_mod.overview(conn, location).photo_id == first
        with write_tx(conn):
            conn.execute("UPDATE photos SET height = NULL WHERE id = ?", (first,))
        assert overview_mod.overview(conn, location) is None
        with write_tx(conn):
            conn.execute("UPDATE captures SET overview_photo_id = NULL")
            conn.execute("UPDATE photos SET height = 10, display_rel_path = 'x.jpg'")
            conn.execute("UPDATE photos SET width = NULL WHERE id = ?", (first,))
        # No named photo: the busiest displayable one (Glue + Rake on the second).
        assert overview_mod.overview(conn, location).photo_id == second


def test_pinned_photo_without_a_display_jpeg_falls_back_and_cannot_be_pinned(auth_settings, garage):
    first, second = garage["photos"]
    with open_db(auth_settings) as conn:
        locations.pin_overview(conn, garage["id"], first)
        with write_tx(conn):
            conn.execute("UPDATE photos SET display_rel_path = NULL WHERE id = ?", (first,))
        view = overview_mod.overview(conn, locations.get(conn, garage["id"]))
        assert view.photo_id == second and not view.pinned
        with pytest.raises(locations.LocationError):
            locations.pin_overview(conn, garage["id"], first)


def test_pin_accepts_extension_photos_and_the_gallery_matches(
    auth_client, auth_settings, garage, tmp_path
):
    ext = add_angle(auth_client, garage["capture"], tmp_path)
    assert run(auth_settings, FakeProvider([result([item("Hose", boxes=[box(2)])])]))
    with open_db(auth_settings) as conn:
        (extra,) = photo_ids(conn, ext)
        with write_tx(conn):
            conn.execute("UPDATE photos SET width = NULL WHERE id = ?", (garage["photos"][0],))
    gallery = f"capture-{garage['capture']}-gallery"
    page = auth_client.get(f"/captures/{garage['capture']}").text
    assert (
        f'id="{gallery}-pin-{extra}"' in page
        and f'id="{gallery}-pin-{garage["photos"][1]}"' in page
    )
    assert f'id="{gallery}-pin-{garage["photos"][0]}"' not in page  # not displayable: no button
    assert f'data-error-target="#{gallery}-pin-msg-{extra}"' in page
    url = f"/l/{garage['code']}/overview"
    pinned = auth_client.post(url, data={"photo_id": str(extra)}, headers=HX)
    assert pinned.status_code == 200
    page = auth_client.get(f"/captures/{garage['capture']}").text
    assert f'id="{gallery}-pin-{extra}"' not in page  # the pinned photo says so instead
    assert page.count('class="muted pin-done">Location photo<') == 1
    assert f'id="{gallery}-pin-{garage["photos"][1]}"' in page
    with open_db(auth_settings) as conn:
        assert overview_mod.overview(conn, locations.get(conn, garage["id"])).photo_id == extra
        with write_tx(conn):
            conn.execute("UPDATE captures SET status = 'failed' WHERE id = ?", (ext,))
        with pytest.raises(locations.LocationError):
            locations.pin_overview(conn, garage["id"], extra)


def test_malformed_photo_param_shows_the_default_photo(auth_client, garage):
    glue = garage["items"]["Glue"]
    for raw in ("abc", "", "1.5", "-1", "9" * 40):
        page = auth_client.get(f"/items/{glue}/photo?photo={raw}")
        assert page.status_code == 200, raw
        assert f'name="photo_id" value="{garage["photos"][0]}"' in page.text


def test_huge_locations_chunk_their_id_lists(auth_client, auth_settings, garage):
    with open_db(auth_settings) as conn:
        many = seed_items(
            conn, [items.ItemData(name=f"Bolt {n}", location_id=garage["id"]) for n in range(1200)]
        )
        conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 600)
        crops = boxes.review_crops(conn, [*many, *garage["items"].values()])
        assert set(crops) == {garage["items"][n] for n in ("Tape", "Glue", "Rake")}
    assert auth_client.get(f"/l/{garage['code']}").status_code == 200


def test_photo_switcher_is_capped_but_keeps_the_selected_photo(auth_client, auth_settings, garage):
    tape = garage["items"]["Tape"]
    with open_db(auth_settings) as conn, write_tx(conn):
        extra = []
        for n in range(60):
            cursor = conn.execute(
                "INSERT INTO photos (capture_id, rel_path, display_rel_path, sha256, width,"
                " height) VALUES (?, 'p.jpg', 'p.jpg', ?, 100, 100)",
                (garage["capture"], f"sha{n}"),
            )
            extra.append(cursor.lastrowid)
            conn.execute(
                "INSERT INTO item_boxes (item_id, photo_id, capture_id, idx, x0, y0, x1, y1)"
                " VALUES (?, ?, ?, 0, 0.1, 0.1, 0.5, 0.5)",
                (tape, cursor.lastrowid, garage["capture"]),
            )
    html = auth_client.get(f"/items/{tape}/photo").text
    assert len(re.findall(r'id="photo-choice-(\d+)" href', html)) == 50
    last = extra[-1]
    html = auth_client.get(f"/items/{tape}/photo?photo={last}").text
    shown = re.findall(r'id="photo-choice-(\d+)" href', html)
    assert len(shown) == 50 and str(last) in shown
    assert f'name="photo_id" value="{last}"' in html


def test_outcome_leaves_out_wrong_detections_but_keeps_used_up(auth_settings, garage):
    from app.capture import service

    with open_db(auth_settings) as conn:
        assert service.outcome(conn, garage["capture"])[0]["n"] == 4
        items.delete_item(conn, garage["items"]["Screws"])
        items.use_up(conn, garage["items"]["Rake"])
        (row,) = service.outcome(conn, garage["capture"])
        assert row["n"] == 3
        for name in ("Tape", "Glue"):
            items.delete_item(conn, garage["items"][name])
        (row,) = service.outcome(conn, garage["capture"])
        assert row["n"] == 1  # only the used-up Rake

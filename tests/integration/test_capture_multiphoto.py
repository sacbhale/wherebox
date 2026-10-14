"""Multi-photo captures (spec-multiphoto Part 2, Part 3, R3, R4, R8, R16): 1-6 photos per
submission, limits enforced before parsing, all-or-nothing saving, one provider call with every
image in order, upright display JPEGs, boxes stored per photo, timeouts and the ledger."""

import hashlib
import io
import threading
from pathlib import Path

import pytest
from PIL import Image
from starlette.requests import Request

from app.ai.fake import FakeProvider
from app.ai.schemas import ExtractedItem
from app.capture import boxes, groups, service
from app.inventory import items, locations
from tests.capture_helpers import (
    FIXTURES,
    ai_calls,
    capture_id,
    capture_items,
    capture_row,
    colour,
    image_colour,
    item,
    make_jpegs,
    post_photo,
    post_photos,
    post_receipt,
    post_text,
    result,
    run,
)
from tests.conftest import TEST_PASSWORD, logged_in
from tests.inventory_helpers import HX, open_db, seed_items


@pytest.fixture
def garage(auth_client, auth_settings) -> int:
    with open_db(auth_settings) as conn:
        location_id, _ = locations.create(conn, "Garage")
    return location_id


def photo_rows(conn, cid: int):
    return conn.execute(
        "SELECT * FROM photos WHERE capture_id = ? ORDER BY position, id", (cid,)
    ).fetchall()


def box_rows(conn, item_id: int):
    return conn.execute(
        "SELECT photo_id, idx, x0, y0, x1, y1 FROM item_boxes WHERE item_id = ?"
        " ORDER BY photo_id, idx",
        (item_id,),
    ).fetchall()


def stored_files(settings) -> list[Path]:
    return (
        [p for p in settings.photos_dir.rglob("*") if p.is_file()]
        if settings.photos_dir.exists()
        else []
    )


def near(actual: tuple[int, ...], expected: tuple[int, ...], tolerance: int = 12) -> bool:
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


@pytest.mark.parametrize("count", [1, 2, 6])
def test_one_to_six_photos_are_stored_in_order_and_sent_in_one_call(
    auth_client, auth_settings, garage, tmp_path, count
):
    paths = make_jpegs(tmp_path, count)
    response = post_photos(auth_client, garage, paths)
    assert response.status_code == 200, response.text
    cid = capture_id(response)
    with open_db(auth_settings) as conn:
        photos = photo_rows(conn, cid)
    assert [p["position"] for p in photos] == list(range(count))
    assert [p["sha256"] for p in photos] == [
        hashlib.sha256(path.read_bytes()).hexdigest() for path in paths
    ]

    provider = FakeProvider([result([item("Tape")])])
    assert run(auth_settings, provider)
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.mode == "shelf" and len(request.images) == count
    for index, data in enumerate(request.images):
        assert near(image_colour(data), colour(index))
    assert request.timeout_s == auth_settings.ai_timeout_seconds + 10 * (count - 1)
    with open_db(auth_settings) as conn:
        (call,) = ai_calls(conn, cid)
        assert call["image_count"] == count
        assert capture_row(conn, cid)["status"] == "done"


def test_seven_photos_are_refused_before_anything_is_saved(
    auth_client, auth_settings, garage, tmp_path
):
    response = post_photos(auth_client, garage, make_jpegs(tmp_path, 7))
    assert response.status_code == 400
    assert "at most 6 photos" in response.text and 'role="alert"' in response.text
    # 8 is the first count the parser itself refuses (max_files=7): same message.
    eight = post_photos(auth_client, garage, make_jpegs(tmp_path / "eight", 8))
    assert eight.status_code == 400 and "at most 6 photos" in eight.text
    many = post_photos(auth_client, garage, make_jpegs(tmp_path / "more", 9))
    assert many.status_code == 400 and "at most 6 photos" in many.text  # parser limit
    with open_db(auth_settings) as conn:
        assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
    assert stored_files(auth_settings) == []


def test_too_many_form_fields_are_refused_by_the_parser(
    auth_client, auth_settings, garage, tmp_path
):
    """max_fields=MAX_FORM_FIELDS bounds the multipart parser: 21 text fields are a 400."""
    data = {"location_id": str(garage)} | {f"f{i}": "x" for i in range(20)}
    (jpeg,) = make_jpegs(tmp_path, 1)
    files = [("photos", (jpeg.name, jpeg.read_bytes(), "image/jpeg"))]
    response = auth_client.post("/captures/photo", headers=HX, data=data, files=files)
    assert response.status_code == 400
    assert "The upload could not be read." in response.text
    with open_db(auth_settings) as conn:
        assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
    assert stored_files(auth_settings) == []


@pytest.mark.parametrize("url", ["/captures/photo", "/captures/reconcile"])
def test_pdfs_are_refused_for_photo_and_reconcile(
    auth_client, auth_settings, garage, tmp_path, url
):
    (jpeg,) = make_jpegs(tmp_path, 1)
    response = post_photos(auth_client, garage, [jpeg, FIXTURES / "receipt.pdf"], url=url)
    assert response.status_code == 400
    assert "Use a photo; PDFs are for receipts." in response.text
    single = post_photos(auth_client, garage, [FIXTURES / "receipt.pdf"], url=url, field="photo")
    assert single.status_code == 400 and "PDFs are for receipts" in single.text
    with open_db(auth_settings) as conn:
        assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 0
    assert stored_files(auth_settings) == []


def test_the_same_photo_twice_is_refused(auth_client, auth_settings, garage, tmp_path):
    first, second = make_jpegs(tmp_path, 2)
    response = post_photos(auth_client, garage, [first, second, first])
    assert response.status_code == 400
    assert "That photo is already in this capture." in response.text
    assert stored_files(auth_settings) == []


def test_saving_is_all_or_nothing(make_settings, tmp_path):
    settings = make_settings(household_password=TEST_PASSWORD, max_upload_mb=1)
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            garage, _ = locations.create(conn, "Garage")
        small = make_jpegs(tmp_path, 2)
        big = tmp_path / "big.jpg"
        big.write_bytes(b"\xff\xd8\xff" + b"0" * (1024 * 1024))
        response = post_photos(client, garage, [small[0], small[1], big])
        assert response.status_code == 413 and "1 MB" in response.text
        with open_db(settings) as conn:
            assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 0
        assert stored_files(settings) == []


def test_content_length_is_checked_before_the_form_is_parsed(
    auth_client, auth_settings, garage, monkeypatch
):
    def no_parsing(self, **kwargs):
        raise AssertionError("the form must not be parsed")

    monkeypatch.setattr(Request, "form", no_parsing)
    limit = (6 * auth_settings.max_upload_mb + 1) * 1024 * 1024
    body = b"x" * 10
    too_big = auth_client.post(
        "/captures/photo",
        headers=HX
        | {"Content-Type": "multipart/form-data; boundary=b", "Content-Length": str(limit + 1)},
        content=body,
    )
    assert too_big.status_code == 413
    assert "larger than 151 MB" in too_big.text and 'role="alert"' in too_big.text

    def chunks():
        yield b"--b\r\n"

    chunked = auth_client.post(
        "/captures/reconcile",
        headers=HX | {"Content-Type": "multipart/form-data; boundary=b"},
        content=chunks(),
    )
    assert chunked.status_code == 411
    assert "Content-Length" in chunked.text
    extend = auth_client.post(
        "/captures/1/extend",
        headers=HX
        | {"Content-Type": "multipart/form-data; boundary=b", "Content-Length": str(limit + 1)},
        content=body,
    )
    assert extend.status_code == 413
    receipt = auth_client.post(
        "/captures/receipt",
        headers=HX | {"Content-Type": "multipart/form-data; boundary=b"},
        content=chunks(),
    )
    assert receipt.status_code == 411


def test_the_single_photo_field_still_works(auth_client, auth_settings, garage):
    response = post_photo(auth_client, garage)
    assert response.status_code == 200
    cid = capture_id(response)
    provider = FakeProvider([result([item("Tape")])])
    run(auth_settings, provider)
    assert len(provider.requests[0].images) == 1
    with open_db(auth_settings) as conn:
        assert capture_row(conn, cid)["status"] == "done"


def test_exif_rotated_photo_boxes_map_onto_the_upright_display_jpeg(
    auth_client, auth_settings, garage
):
    """R16: the red block of rotated.jpg is upright at [ymin, xmin, ymax, xmax] = [0, 750,
    250, 1000]; the box lands on it in the display JPEG and in the image sent to the model."""
    response = post_photos(auth_client, garage, [FIXTURES / "rotated.jpg"])
    cid = capture_id(response)
    red_box = {"image": 0, "box_2d": [0, 750, 250, 1000]}
    provider = FakeProvider([result([item("Red block", boxes=[red_box])])])
    run(auth_settings, provider)
    with open_db(auth_settings) as conn:
        (photo,) = photo_rows(conn, cid)
        (row,) = capture_items(conn, cid)
        (box,) = box_rows(conn, row["id"])
    assert (photo["width"], photo["height"]) == (1200, 1600)  # upright display size
    assert row["photo_id"] == photo["id"]
    assert (box["x0"], box["y0"], box["x1"], box["y1"]) == (0.75, 0.0, 1.0, 0.25)
    display = auth_settings.data_dir / photo["display_rel_path"]
    with Image.open(display) as im:
        assert im.size == (1200, 1600)
        assert not im.getexif().get(0x0112)  # no orientation left to apply
        rgb = im.convert("RGB")
        centre = (round((box["x0"] + box["x1"]) / 2 * im.width), round(box["y1"] / 2 * im.height))
        assert near(rgb.getpixel(centre), (230, 30, 30))
        outside = (round(0.25 * im.width), round(0.6 * im.height))
        assert near(rgb.getpixel(outside), (40, 90, 160))
    with Image.open(io.BytesIO(provider.requests[0].images[0])) as sent:
        assert sent.size[1] > sent.size[0]  # portrait, like the display JPEG
        assert near(
            sent.convert("RGB").getpixel((round(0.875 * sent.width), round(0.125 * sent.height))),
            (230, 30, 30),
        )


def test_heic_display_jpeg_is_upright_and_sized(auth_client, auth_settings, garage):
    cid = capture_id(post_photos(auth_client, garage, [FIXTURES / "iphone.heic"]))
    run(auth_settings, FakeProvider([result([item("Thing")])]))
    with open_db(auth_settings) as conn:
        (photo,) = photo_rows(conn, cid)
    assert photo["display_rel_path"].endswith(".display.jpg")
    with Image.open(auth_settings.data_dir / photo["display_rel_path"]) as im:
        assert im.format == "JPEG" and im.size == (1600, 2000)
    assert (photo["width"], photo["height"]) == (1600, 2000)


def test_boxes_are_stored_per_photo_and_pick_the_items_photo(
    auth_client, auth_settings, garage, tmp_path
):
    cid = capture_id(post_photos(auth_client, garage, make_jpegs(tmp_path, 3)))
    provider = FakeProvider(
        [
            result(
                [
                    item(
                        "Tape",
                        boxes=[
                            {"image": 0, "box_2d": [100, 100, 200, 300]},
                            {"image": 2, "box_2d": [500, 500, 600, 700]},
                            {"image": 2, "box_2d": [0, 0, 10, 10]},
                        ],
                    ),
                    item("Glue", boxes=[{"image": 1, "box_2d": [0, 0, 500, 500]}]),
                    item("Nails"),  # not marked
                ]
            )
        ]
    )
    run(auth_settings, provider)
    with open_db(auth_settings) as conn:
        photos = [p["id"] for p in photo_rows(conn, cid)]
        by_name = {r["name"]: r for r in capture_items(conn, cid)}
        tape = box_rows(conn, by_name["Tape"]["id"])
        glue = box_rows(conn, by_name["Glue"]["id"])
        nails = box_rows(conn, by_name["Nails"]["id"])
        box_capture = {r[0] for r in conn.execute("SELECT DISTINCT capture_id FROM item_boxes")}
    assert [(b["photo_id"], b["idx"]) for b in tape] == [
        (photos[0], 0),
        (photos[2], 0),
        (photos[2], 1),
    ]
    assert (tape[0]["x0"], tape[0]["y0"], tape[0]["x1"], tape[0]["y1"]) == (0.1, 0.1, 0.3, 0.2)
    assert [b["photo_id"] for b in glue] == [photos[1]]
    assert nails == []
    assert by_name["Tape"]["photo_id"] == photos[0]
    assert by_name["Glue"]["photo_id"] == photos[1]  # first photo with one of its boxes
    assert by_name["Nails"]["photo_id"] == photos[0]  # unmarked: the call's first photo
    assert box_capture == {cid}


def test_malformed_boxes_are_dropped_and_the_capture_still_succeeds(
    auth_client, auth_settings, garage, tmp_path
):
    cid = capture_id(post_photos(auth_client, garage, make_jpegs(tmp_path, 2)))
    junk = ExtractedItem.model_validate(
        {
            "name": "Hammer",
            "quantity": 1,
            "kind": "belonging",
            "confidence": 0.9,
            "boxes": [
                {"image": 5, "box_2d": [0, 0, 100, 100]},  # no such image
                {"image": 0, "box_2d": [0, 0, 100]},
                {"image": 1, "box_2d": "everywhere"},
                "left",
            ],
        }
    )
    silly = ExtractedItem.model_validate(
        {"name": "Saw", "quantity": 1, "kind": "belonging", "confidence": 0.9, "boxes": "yes"}
    )
    run(auth_settings, FakeProvider([result([junk, silly])]))
    with open_db(auth_settings) as conn:
        assert capture_row(conn, cid)["status"] == "done"
        rows = capture_items(conn, cid)
        assert [r["name"] for r in rows] == ["Hammer", "Saw"]
        assert conn.execute("SELECT count(*) FROM item_boxes").fetchone()[0] == 0


def test_receipts_and_text_never_store_boxes(auth_client, auth_settings):
    post_receipt(auth_client)
    post_text(auth_client, "a hammer")
    boxed = item("Milk", boxes=[{"image": 0, "box_2d": [0, 0, 100, 100]}])
    provider = FakeProvider([result([boxed]), result([boxed])])
    run(auth_settings, provider)
    run(auth_settings, provider)
    assert provider.requests[0].timeout_s == auth_settings.ai_timeout_seconds
    with open_db(auth_settings) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM item_boxes").fetchone()[0] == 0
        calls = ai_calls(conn)
    assert [c["image_count"] for c in calls] == [1, 0]


def test_reconcile_takes_several_photos_and_writes_boxes(auth_client, auth_settings, tmp_path):
    with open_db(auth_settings) as conn:
        shelf, _ = locations.create(conn, "Shelf")
        tape, glue = seed_items(
            conn,
            [
                items.ItemData(name="Tape", location_id=shelf, quantity=2),
                items.ItemData(name="Glue", location_id=shelf, quantity=1),
            ],
        )
    first = post_photos(auth_client, shelf, make_jpegs(tmp_path, 2), url="/captures/reconcile")
    cid = capture_id(first)
    box = {"image": 1, "box_2d": [0, 0, 500, 500]}
    provider = FakeProvider(
        [
            result(
                [
                    item("Tape", quantity=3, matched_existing_id=tape, boxes=[box]),
                    item("Saw", boxes=[{"image": 0, "box_2d": [10, 10, 20, 20]}]),
                ]
            )
        ]
    )
    run(auth_settings, provider)
    assert len(provider.requests[0].images) == 2
    with open_db(auth_settings) as conn:
        photos = [p["id"] for p in photo_rows(conn, cid)]
        (saw,) = capture_items(conn, cid)
        assert [b["photo_id"] for b in box_rows(conn, tape)] == [photos[1]]
        assert [b["photo_id"] for b in box_rows(conn, saw["id"])] == [photos[0]]
        assert box_rows(conn, glue) == []
        proposals = conn.execute(
            "SELECT type, item_id, proposed_quantity FROM review_proposals ORDER BY id"
        ).fetchall()
        tape_row = items.get(conn, tape)
    # Unchanged reconcile semantics: count differs -> quantity proposal; unseen -> missing.
    assert [tuple(p) for p in proposals] == [("quantity", tape, 3), ("missing", glue, None)]
    assert tape_row["quantity"] == 2 and tape_row["last_seen_at"] is not None


def test_group_view_and_sightings_of_a_multiphoto_capture(
    auth_client, auth_settings, garage, tmp_path
):
    cid = capture_id(post_photos(auth_client, garage, make_jpegs(tmp_path, 2)))
    provider = FakeProvider(
        [
            result(
                [
                    item("Right", boxes=[{"image": 0, "box_2d": [0, 600, 100, 900]}]),
                    item("Left", boxes=[{"image": 0, "box_2d": [0, 100, 100, 200]}]),
                    item("Second", boxes=[{"image": 1, "box_2d": [100, 100, 900, 900]}]),
                    item("Unmarked"),
                ]
            )
        ]
    )
    run(auth_settings, provider)
    with open_db(auth_settings) as conn:
        view = groups.group(conn, cid)
        by_name = {r["name"]: r["id"] for r in capture_items(conn, cid)}
        seen = boxes.sightings(conn, by_name["Second"])
        fallback = boxes.sightings(conn, by_name["Unmarked"])
        crops = boxes.review_crops(conn, by_name.values())
    assert view is not None and view.root_id == cid and view.location_id == garage
    assert [p.index for p in view.photos] == [0, 1]
    assert view.photos[0].url == f"/photos/{view.photos[0].id}"
    assert (view.photos[0].width, view.photos[0].height) == (320, 240)
    assert [(i.number, i.name, i.state) for i in view.items] == [
        (1, "Left", "here"),
        (2, "Right", "here"),
        (3, "Second", "here"),
        (4, "Unmarked", "here"),
    ]
    assert not view.items[3].marked and view.items[0].marked
    assert view.items[2].boxes_on(view.photos[1].id)[0].x0 == 0.1
    assert view.photo_capacity == 10
    assert [s.photo_id for s in seen] == [view.photos[1].id]
    assert seen[0].location_name == "Garage" and seen[0].root_capture_id == cid
    assert seen[0].boxes[0].y1 == 0.9 and seen[0].marked
    assert [(s.photo_id, s.marked) for s in fallback] == [(view.photos[0].id, False)]
    assert set(crops) == {by_name["Right"], by_name["Left"], by_name["Second"]}
    crop = crops[by_name["Second"]]
    # 0.1-0.9 padded to 0.96 x 0.96 of a 320x240 photo: 307 px wide, so 307 px high is
    # more than the photo: the whole height.
    assert (crop.x0, crop.y0, crop.x1, crop.y1) == pytest.approx((0.02, 0.0, 0.98, 1.0))
    assert crop.photo_id == view.photos[1].id and crop.root_capture_id == cid
    with open_db(auth_settings) as conn:
        assert groups.group(conn, 999_999) is None


def test_concurrent_submissions_are_all_stored(auth_client, auth_settings, garage, tmp_path):
    folders = [make_jpegs(tmp_path / str(n), 2, size=(200 + n, 100)) for n in range(3)]
    responses = []

    def submit(paths):
        responses.append(post_photos(auth_client, garage, paths))

    threads = [threading.Thread(target=submit, args=(paths,)) for paths in folders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(r.status_code for r in responses) == [200, 200, 200]
    with open_db(auth_settings) as conn:
        assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 6
        assert service.get(conn, capture_id(responses[0]))["photo_count"] == 2


def test_an_unreadable_photo_fails_the_capture_before_any_call(
    auth_client, auth_settings, garage, tmp_path
):
    (good,) = make_jpegs(tmp_path, 1)
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff" + b"not really a jpeg" * 10)
    cid = capture_id(post_photos(auth_client, garage, [good, broken]))
    provider = FakeProvider([result([item("Tape")])])
    run(auth_settings, provider)
    assert provider.requests == []
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        first, second = photo_rows(conn, cid)
        assert ai_calls(conn, cid) == []
    assert capture["status"] == "failed" and "not a readable image" in capture["error"]
    assert first["display_rel_path"] is not None and first["width"] == 320  # kept for a retry
    assert second["display_rel_path"] is None


@pytest.mark.parametrize(
    "url", ["/captures/photo", "/captures/reconcile", "/captures/receipt", "/captures/1/extend"]
)
def test_upload_routes_refuse_non_multipart_bodies_unparsed(
    auth_client, auth_settings, garage, monkeypatch, url
):
    def no_parsing(self, **kwargs):
        raise AssertionError("the form must not be parsed")

    monkeypatch.setattr(Request, "form", no_parsing)
    response = auth_client.post(url, headers=HX, data={"location_id": str(garage)})
    assert response.status_code == 415
    assert "file upload" in response.text and 'role="alert"' in response.text
    with open_db(auth_settings) as conn:
        assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0


@pytest.mark.parametrize(("kind", "name"), [("BMP", "photo.jpg"), ("TIFF", "photo.tiff")])
def test_other_image_formats_are_refused_without_decoding(
    auth_client, auth_settings, garage, tmp_path, kind, name
):
    """Only the formats phones and browsers produce are decoded: a BMP renamed .jpg (or a
    TIFF) is stored, then fails cleanly with a message, before any provider call."""
    path = tmp_path / name
    Image.new("RGB", (64, 48), (200, 30, 30)).save(path, format=kind)
    cid = capture_id(post_photos(auth_client, garage, [path]))
    provider = FakeProvider([result([item("Tape")])])
    assert run(auth_settings, provider)
    assert provider.requests == []
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        (photo,) = photo_rows(conn, cid)
    assert capture["status"] == "failed" and "not a readable image" in capture["error"]
    assert photo["display_rel_path"] is None
    assert Path(photo["rel_path"]).suffix == (".jpg" if name.endswith(".jpg") else ".bin")


def test_photos_are_cached_briefly_until_their_display_jpeg_exists(
    auth_client, auth_settings, garage, tmp_path
):
    cid = capture_id(post_photos(auth_client, garage, make_jpegs(tmp_path, 1)))
    with open_db(auth_settings) as conn:
        (photo,) = photo_rows(conn, cid)
    before = auth_client.get(f"/photos/{photo['id']}")
    assert before.status_code == 200
    assert before.headers["cache-control"] == "private, max-age=60"
    run(auth_settings, FakeProvider([result([])]))
    after = auth_client.get(f"/photos/{photo['id']}")
    assert after.headers["cache-control"] == "private, max-age=86400"

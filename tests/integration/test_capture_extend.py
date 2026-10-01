""" "Add another angle" extension captures (spec-multiphoto Part 4, R5-R7): root resolution,
inherited location, the group's photos and existing items (i)+(ii) in one call, matched items
never edited (boxes replaced, last_seen, missing dismissed, quantity proposal only when
higher), gone items never re-added, the name safety net, refusals, the 12-photo group limit
(also under concurrent submissions) and the group read model."""

import re
import threading
from contextlib import closing
from datetime import UTC, datetime

import pytest

from app.ai.fake import FakeProvider
from app.ai.provider import ProviderPermanentError
from app.capture import groups, reconcile, service
from app.db.connection import connect, write_tx
from app.inventory import items, locations
from tests.capture_helpers import (
    FIXTURES,
    capture_id,
    capture_items,
    capture_row,
    colour,
    image_colour,
    item,
    make_jpegs,
    post_extend,
    post_photos,
    post_receipt,
    result,
    run,
)
from tests.conftest import TEST_PASSWORD, logged_in
from tests.integration.test_auth import PROTECTED
from tests.inventory_helpers import HX, open_db, seed_items


def box(image: int, top: int = 100) -> dict[str, object]:
    return {"image": image, "box_2d": [top, 100, top + 200, 400]}


@pytest.fixture
def places(auth_client, auth_settings) -> dict[str, int]:
    with open_db(auth_settings) as conn:
        garage, _ = locations.create(conn, "Garage")
        shed, _ = locations.create(conn, "Shed")
    return {"garage": garage, "shed": shed}


def make_root(client, settings, location_id, tmp_path, answer, count=2) -> int:
    cid = capture_id(post_photos(client, location_id, make_jpegs(tmp_path / "root", count)))
    assert run(settings, FakeProvider([answer]))
    with open_db(settings) as conn:
        assert capture_row(conn, cid)["status"] == "done"
    return cid


def photo_ids(conn, cid: int) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT id FROM photos WHERE capture_id = ? ORDER BY position, id", (cid,)
        )
    ]


def boxes_of(conn, item_id: int) -> list[tuple]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT photo_id, idx, x0, y0, x1, y1, capture_id FROM item_boxes WHERE item_id = ?"
            " ORDER BY photo_id, idx",
            (item_id,),
        )
    ]


def named(conn, cid: int) -> dict[str, int]:
    return {r["name"]: r["id"] for r in capture_items(conn, cid)}


def test_extend_route_is_protected():
    assert ("POST", "/captures/1/extend") in PROTECTED


def test_extension_sends_the_whole_group_and_existing_items(
    auth_client, auth_settings, places, tmp_path
):
    garage = places["garage"]
    root = make_root(
        auth_client,
        auth_settings,
        garage,
        tmp_path,
        result([item(n, quantity=q) for n, q in [("Tape", 2), ("Rake", 1), ("Soap", 3)]]),
    )
    with open_db(auth_settings) as conn:
        ids = named(conn, root)
        (drill,) = seed_items(conn, [items.ItemData(name="Drill", location_id=garage)])
        items.move_item(conn, ids["Rake"], places["shed"])
        items.use_up(conn, ids["Soap"])

    new = make_jpegs(tmp_path / "ext", 1, size=(300, 200))
    response = post_extend(auth_client, root, new)
    assert response.status_code == 200, response.text
    ext = capture_id(response)
    # The usual status partial, polling in the group page's view.
    assert f'hx-get="/captures/{ext}/status?in=group"' in response.text
    with open_db(auth_settings) as conn:
        row = capture_row(conn, ext)
    assert (row["type"], row["parent_capture_id"], row["location_id"]) == ("photo", root, garage)

    provider = FakeProvider([result([]), result([])])
    assert run(auth_settings, provider)
    request = provider.requests[0]
    assert request.mode == "extend"
    assert len(request.images) == 3
    assert [image_colour(i) for i in request.images[:2]] == [
        pytest.approx(colour(0), abs=12),
        pytest.approx(colour(1), abs=12),
    ]
    assert request.timeout_s == auth_settings.ai_timeout_seconds + 20
    sent = [(e.id, e.name, e.gone) for e in request.existing_items]
    assert sent == [
        (ids["Tape"], "Tape", False),
        (drill, "Drill", False),
        (ids["Rake"], "Rake", True),
        (ids["Soap"], "Soap", True),
    ]

    # An extension of the extension attaches to the root and sends every done photo.
    second = capture_id(
        post_extend(auth_client, ext, make_jpegs(tmp_path / "ext2", 2, size=(330, 200)))
    )
    with open_db(auth_settings) as conn:
        assert capture_row(conn, second)["parent_capture_id"] == root
    assert run(auth_settings, provider)
    assert len(provider.requests[1].images) == 5
    with open_db(auth_settings) as conn:
        assert groups.call_photos(conn, capture_row(conn, second))[-1]["capture_id"] == second
        calls = conn.execute(
            "SELECT image_count FROM ai_calls WHERE capture_id IN (?, ?) ORDER BY id",
            (ext, second),
        ).fetchall()
    assert [c[0] for c in calls] == [3, 5]


def test_matched_items_are_never_edited(auth_client, auth_settings, places, tmp_path):
    garage = places["garage"]
    root = make_root(
        auth_client,
        auth_settings,
        garage,
        tmp_path,
        result(
            [
                item("Tape", quantity=2, boxes=[box(0)]),
                item("Glue", quantity=1, boxes=[box(1)]),
                item("Screws", quantity=10),
                item("Rake", quantity=1, boxes=[box(0, 600)]),
            ]
        ),
    )
    with open_db(auth_settings) as conn:
        ids = named(conn, root)
        items.move_item(conn, ids["Rake"], places["shed"])
        with write_tx(conn):
            reconcile.propose(conn, "missing", ids["Glue"], root)
        before = {
            r["id"]: tuple(r)
            for r in conn.execute(
                "SELECT id, name, quantity, category_id, kind, location_id, updated_at, deleted_at"
                " FROM items"
            )
        }
        root_photos = photo_ids(conn, root)
    ext = capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "ext", 1, (300, 200))))
    answer = result(
        [
            item("Tape", quantity=1, matched_existing_id=ids["Tape"], boxes=[box(0, 50), box(2)]),
            item("Glue", quantity=3, matched_existing_id=ids["Glue"], boxes=[box(2, 300)]),
            item("Rake", matched_existing_id=ids["Rake"], boxes=[box(2, 500)]),
            item("screws ", quantity=12),  # unmatched, same name: safety net
            item("Saw", boxes=[box(2, 700)]),
            item("Ghost", matched_existing_id=999_999),  # not sent: unmatched
        ]
    )
    provider = FakeProvider([answer])
    assert run(auth_settings, provider)
    with open_db(auth_settings) as conn:
        assert capture_row(conn, ext)["status"] == "done"
        (new_photo,) = photo_ids(conn, ext)
        created = named(conn, ext)
        after = {
            r["id"]: tuple(r)
            for r in conn.execute(
                "SELECT id, name, quantity, category_id, kind, location_id, updated_at, deleted_at"
                " FROM items"
            )
            if r["id"] in before
        }
        proposals = [
            tuple(r)
            for r in conn.execute(
                "SELECT type, item_id, proposed_quantity, status FROM review_proposals ORDER BY id"
            )
        ]
        tape = items.get(conn, ids["Tape"])
        glue = items.get(conn, ids["Glue"])
        screws = items.get(conn, ids["Screws"])
        rake = items.get(conn, ids["Rake"])
        tape_boxes = boxes_of(conn, ids["Tape"])
        glue_boxes = boxes_of(conn, ids["Glue"])
        rake_boxes = boxes_of(conn, ids["Rake"])
        saw = items.get(conn, created["Saw"])
        outcome = groups.extension_outcome(conn, ext)
        view = groups.group(conn, ext)

    assert after == before  # name, quantity, category, location, updated_at untouched
    assert sorted(created) == ["Ghost", "Saw"]  # only the unmatched ones were inserted
    assert saw["photo_id"] == new_photo and saw["location_id"] == garage
    assert tape["last_seen_at"] is not None and glue["last_seen_at"] is not None
    assert rake["last_seen_at"] is None  # (ii): boxes only
    assert rake["location_id"] == places["shed"]
    # Tape: boxes on the call's photos replaced (photo 0 moved, photo 2 added).
    assert [(b[0], b[3], b[6]) for b in tape_boxes] == [
        (root_photos[0], 0.05, ext),
        (new_photo, 0.1, ext),
    ]
    # Per-photo replace: Glue kept its root box on photo 1 (the model only marked it on the
    # new photo); Rake kept its root box on photo 0 and gained one on the new photo.
    assert [(b[0], b[6]) for b in glue_boxes] == [(root_photos[1], root), (new_photo, ext)]
    assert [(b[0], b[3], b[6]) for b in rake_boxes] == [
        (root_photos[0], 0.6, root),
        (new_photo, 0.5, ext),
    ]
    # Lower count ignored; higher count proposed; missing proposal dismissed.
    assert proposals == [
        ("missing", ids["Glue"], None, "dismissed"),
        ("quantity", ids["Glue"], 3, "open"),
        ("quantity", ids["Screws"], 12, "open"),  # safety-net match counts as matched
    ]
    # Safety net: matched by name, flagged, never duplicated.
    assert screws["needs_review"] == 1 and screws["quantity"] == 10
    assert tape["needs_review"] == 0
    assert outcome == (2, 3)  # Saw + Ghost new; Tape, Glue, Screws matched (Rake is gone)
    states = {i.name: (i.state, i.location_name) for i in view.items}
    assert states["Rake"] == ("moved", "Shed")
    assert states["Tape"] == ("here", "Garage")
    assert [p.capture_id for p in view.photos] == [root, root, ext]
    assert [c["id"] for c in view.captures] == [root, ext]
    assert view.photo_capacity == 9


def test_used_up_group_items_are_sent_as_gone_and_never_re_added(
    auth_client, auth_settings, places, tmp_path
):
    root = make_root(
        auth_client,
        auth_settings,
        places["garage"],
        tmp_path,
        result([item("Soap", boxes=[box(0)])]),
    )
    with open_db(auth_settings) as conn:
        soap = named(conn, root)["Soap"]
        items.use_up(conn, soap)
    ext = capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "e", 1, (300, 200))))
    answer = item("Soap", matched_existing_id=soap, boxes=[box(0, 300), box(1)])
    provider = FakeProvider([result([answer])])
    run(auth_settings, provider)
    assert [(e.id, e.gone) for e in provider.requests[0].existing_items] == [(soap, True)]
    with open_db(auth_settings) as conn:
        assert capture_items(conn, ext) == []
        # Still annotated on the old and the new photo (its boxes for this call replaced).
        assert [(b[3], b[6]) for b in boxes_of(conn, soap)] == [(0.3, ext), (0.1, ext)]
        assert items.get(conn, soap) is None  # still used up
        view = groups.group(conn, root)
    assert [(i.name, i.state) for i in view.items] == [("Soap", "used_up")]


def test_extension_of_a_removed_location_goes_to_the_nearest_live_ancestor(
    auth_client, auth_settings, tmp_path
):
    with open_db(auth_settings) as conn:
        room, _ = locations.create(conn, "Room")
        shelf, _ = locations.create(conn, "Shelf", room)
    root = make_root(auth_client, auth_settings, shelf, tmp_path, result([item("Tape")]))
    with open_db(auth_settings) as conn:
        locations.delete(conn, shelf)
    ext = capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "e", 1, (300, 200))))
    run(auth_settings, FakeProvider([result([item("Saw")])]))
    with open_db(auth_settings) as conn:
        (saw,) = capture_items(conn, ext)
        assert capture_row(conn, ext)["location_id"] == shelf
    assert saw["location_id"] == room and saw["needs_review"] == 1


def test_only_done_photo_captures_can_be_extended(auth_client, auth_settings, places, tmp_path):
    photo = [FIXTURES / "shelf.jpg"]
    assert post_extend(auth_client, 999_999, photo).status_code == 404
    queued = capture_id(post_photos(auth_client, places["garage"], make_jpegs(tmp_path / "q", 1)))
    refused = post_extend(auth_client, queued, photo)
    assert refused.status_code == 409 and "Only a finished capture" in refused.text
    run(auth_settings, FakeProvider([ProviderPermanentError("nope")]))
    with open_db(auth_settings) as conn:
        assert capture_row(conn, queued)["status"] == "failed"
    assert post_extend(auth_client, queued, photo).status_code == 409
    receipt = capture_id(post_receipt(auth_client))
    run(auth_settings, FakeProvider([result([item("Milk")])]))
    wrong = post_extend(auth_client, receipt, photo)
    assert wrong.status_code == 409 and "Only photo captures" in wrong.text
    reconciled = capture_id(
        post_photos(
            auth_client, places["garage"], make_jpegs(tmp_path / "r", 1), url="/captures/reconcile"
        )
    )
    run(auth_settings, FakeProvider([result([])]))
    with open_db(auth_settings) as conn:
        assert capture_row(conn, reconciled)["status"] == "done"
    assert post_extend(auth_client, reconciled, photo).status_code == 409
    done = make_root(auth_client, auth_settings, places["garage"], tmp_path, result([]), count=1)
    empty = post_extend(auth_client, done, [])
    assert empty.status_code == 400 and "Choose a photo" in empty.text
    pdf = post_extend(auth_client, done, [FIXTURES / "receipt.pdf"])
    assert pdf.status_code == 400 and "PDFs are for receipts" in pdf.text
    with open_db(auth_settings) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM captures WHERE parent_capture_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )


def test_extension_respects_the_ai_gates(make_settings, tmp_path):
    settings = make_settings(household_password=TEST_PASSWORD, ai_provider="anthropic")
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            garage, _ = locations.create(conn, "Garage")
            with FIXTURES.joinpath("shelf.jpg").open("rb") as fh:
                saved = service.save_upload(settings, fh, "shelf.jpg", datetime.now(UTC))
            root = service.create_capture(
                conn,
                capture_type="photo",
                now=datetime.now(UTC),
                location_id=garage,
                uploads=[saved],
            )
        run(settings, FakeProvider([result([])]))
        response = post_extend(client, root, make_jpegs(tmp_path, 1))
    assert response.status_code == 409 and "AI capture is disabled" in response.text


def test_group_limit_of_twelve_photos(auth_client, auth_settings, places, tmp_path):
    root = make_root(auth_client, auth_settings, places["garage"], tmp_path, result([]), count=6)
    four = post_extend(auth_client, root, make_jpegs(tmp_path / "a", 4, (301, 200)))
    assert four.status_code == 200  # queued: counts toward the limit
    three = post_extend(auth_client, root, make_jpegs(tmp_path / "b", 3, (302, 200)))
    assert three.status_code == 400
    assert "at most 12 photos: there is room for 2 more." in three.text
    duplicate = post_extend(auth_client, root, [tmp_path / "root" / "view0.jpg"])
    assert duplicate.status_code == 400 and "already in this capture" in duplicate.text
    two = post_extend(auth_client, root, make_jpegs(tmp_path / "c", 2, (303, 200)))
    assert two.status_code == 200
    full = post_extend(auth_client, root, make_jpegs(tmp_path / "d", 1, (304, 200)))
    assert full.status_code == 400 and "this one is full" in full.text
    with open_db(auth_settings) as conn:
        assert conn.execute("SELECT count(*) FROM photos").fetchone()[0] == 12
    files = [p for p in auth_settings.photos_dir.rglob("*") if p.is_file()]
    assert len(files) == 6 + 6 + 6  # 6 root originals + their display JPEGs, 6 extension photos


def test_concurrent_extensions_cannot_exceed_the_group_limit(
    auth_client, auth_settings, places, tmp_path
):
    root = make_root(auth_client, auth_settings, places["garage"], tmp_path, result([]), count=6)
    now = datetime.now(UTC)
    batches = []
    for n in range(2):
        saved = []
        for path in make_jpegs(tmp_path / f"t{n}", 4, (310 + n, 200)):
            with path.open("rb") as fh:
                saved.append(service.save_upload(auth_settings, fh, path.name, now))
        batches.append(saved)
    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def submit(uploads):
        with closing(connect(auth_settings.db_path)) as conn:
            barrier.wait()
            try:
                outcomes.append(
                    service.create_capture(
                        conn, capture_type="photo", now=now, uploads=uploads, extends=root
                    )
                )
            except service.UploadError as exc:
                outcomes.append(exc)

    threads = [threading.Thread(target=submit, args=(batch,)) for batch in batches]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    created = [o for o in outcomes if isinstance(o, int)]
    refused = [o for o in outcomes if isinstance(o, service.UploadError)]
    assert len(created) == 1 and len(refused) == 1
    assert "room for 2 more" in str(refused[0])
    with open_db(auth_settings) as conn:
        assert len(groups.counted_photos(conn, root)) == 10


def test_failed_extensions_do_not_count_and_are_not_sent(
    auth_client, auth_settings, places, tmp_path
):
    root = make_root(auth_client, auth_settings, places["garage"], tmp_path, result([]), count=6)
    failed = capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "f", 6, (320, 200))))
    run(auth_settings, FakeProvider([ProviderPermanentError("nope")]))
    with open_db(auth_settings) as conn:
        assert capture_row(conn, failed)["status"] == "failed"
    ok = capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "g", 6, (321, 200))))
    provider = FakeProvider([result([])])
    run(auth_settings, provider)
    assert len(provider.requests[0].images) == 12  # root 6 + its own 6; the failed one left out
    with open_db(auth_settings) as conn:
        assert capture_row(conn, ok)["status"] == "done"
        assert groups.group(conn, root).photo_capacity == 0


def test_retry_of_a_failed_extension_rechecks_the_group(
    auth_client, auth_settings, places, tmp_path
):
    """Root 6 + A (6, failed: its photos stop counting) + B (the same 6, done): retrying A
    would make 18 photos with repeats, so it is refused (409 message) and A stays failed."""
    root = make_root(auth_client, auth_settings, places["garage"], tmp_path, result([]), count=6)
    six = make_jpegs(tmp_path / "a", 6, (320, 200))
    first = capture_id(post_extend(auth_client, root, six))
    run(auth_settings, FakeProvider([ProviderPermanentError("nope")]))
    second = capture_id(post_extend(auth_client, root, six))
    run(auth_settings, FakeProvider([result([])]))
    with open_db(auth_settings) as conn:
        assert capture_row(conn, first)["status"] == "failed"
        assert capture_row(conn, second)["status"] == "done"

    status = auth_client.get(f"/captures/{first}/status").text
    retry = re.search(r"<button[^>]*/retry[^>]*>", status).group(0)
    assert "data-show-errors" in retry
    assert f'data-error-target="#capture-{first}-retry-message"' in retry
    assert f'id="capture-{first}-retry-message"' in status

    refused = auth_client.post(f"/captures/{first}/retry", headers=HX)
    assert refused.status_code == 409
    assert "This capture is full now — add the photos as a new angle." in refused.text
    queue = auth_client.post(f"/review/captures/{first}/retry", headers=HX)
    assert queue.status_code == 409 and "This capture is full now" in queue.text
    review = auth_client.get("/review").text
    button = re.search(rf"<button[^>]*/review/captures/{first}/retry[^>]*>", review).group(0)
    assert "data-show-errors" in button
    assert f'id="review-capture-{first}-message"' in review
    with open_db(auth_settings) as conn:
        assert capture_row(conn, first)["status"] == "failed"
        assert len(groups.counted_photos(conn, root)) == 12
    assert not run(auth_settings, FakeProvider([]))  # nothing was queued


def test_retry_refuses_photos_already_in_the_group(auth_client, auth_settings, places, tmp_path):
    root = make_root(auth_client, auth_settings, places["garage"], tmp_path, result([]), count=2)
    two = make_jpegs(tmp_path / "a", 2, (320, 200))
    first = capture_id(post_extend(auth_client, root, two))
    run(auth_settings, FakeProvider([ProviderPermanentError("nope")]))
    capture_id(post_extend(auth_client, root, two))
    run(auth_settings, FakeProvider([result([])]))
    refused = auth_client.post(f"/captures/{first}/retry", headers=HX)
    assert refused.status_code == 409
    assert "Those photos are already in this capture." in refused.text

    # A failed extension that still fits is re-queued as before.
    other = capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "b", 2, (330, 200))))
    run(auth_settings, FakeProvider([ProviderPermanentError("nope")]))
    assert auth_client.post(f"/captures/{other}/retry", headers=HX).status_code == 200
    with open_db(auth_settings) as conn:
        assert capture_row(conn, other)["status"] == "queued"


def test_worker_fails_a_capture_with_too_many_group_photos(
    auth_client, auth_settings, places, tmp_path
):
    """Safety net behind create_capture and retry: a call never sends more than 12 photos."""
    root = make_root(auth_client, auth_settings, places["garage"], tmp_path, result([]), count=6)
    first = capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "a", 6, (320, 200))))
    run(auth_settings, FakeProvider([ProviderPermanentError("nope")]))
    capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "b", 6, (330, 200))))
    run(auth_settings, FakeProvider([result([])]))
    with open_db(auth_settings) as conn, write_tx(conn):  # re-queued behind retry's back
        conn.execute("UPDATE captures SET status = 'queued' WHERE id = ?", (first,))
    provider = FakeProvider([result([])])
    assert run(auth_settings, provider)
    assert provider.requests == []
    with open_db(auth_settings) as conn:
        row = capture_row(conn, first)
    assert row["status"] == "failed" and "more than 12 photos" in row["error"]


def test_name_match_to_a_gone_item_is_a_new_item_for_review(
    auth_client, auth_settings, places, tmp_path
):
    """The safety net only catches live items: a replacement for a used-up item is added
    (flagged), and the used-up one stays used up."""
    root = make_root(
        auth_client, auth_settings, places["garage"], tmp_path, result([item("Soap"), item("Tape")])
    )
    with open_db(auth_settings) as conn:
        ids = named(conn, root)
        items.use_up(conn, ids["Soap"])
    ext = capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "e", 1, (300, 200))))
    run(auth_settings, FakeProvider([result([item("soap"), item("tape ")])]))
    with open_db(auth_settings) as conn:
        created = capture_items(conn, ext)
        assert items.get(conn, ids["Soap"]) is None
        tape = items.get(conn, ids["Tape"])
    assert [(r["name"], r["needs_review"]) for r in created] == [("soap", 1)]
    assert tape["needs_review"] == 1  # live name match: matched and flagged


def test_extension_outcome_counts_every_live_match(auth_client, auth_settings, places, tmp_path):
    """ "Y already known" = live items matched, boxed or not (safety net included); a gone
    match with boxes does not count."""
    root = make_root(
        auth_client,
        auth_settings,
        places["garage"],
        tmp_path,
        result([item("Tape"), item("Glue"), item("Rake")]),
    )
    with open_db(auth_settings) as conn:
        ids = named(conn, root)
        items.move_item(conn, ids["Rake"], places["shed"])
    ext = capture_id(post_extend(auth_client, root, make_jpegs(tmp_path / "e", 1, (300, 200))))
    answer = [
        item("Tape", matched_existing_id=ids["Tape"]),  # no boxes
        item("glue"),  # safety net, no boxes
        item("Rake", matched_existing_id=ids["Rake"], boxes=[box(2)]),  # gone
        item("Saw"),
    ]
    run(auth_settings, FakeProvider([result(answer)]))
    with open_db(auth_settings) as conn:
        assert groups.extension_outcome(conn, ext) == (1, 2)
    status = auth_client.get(f"/captures/{ext}/status").text
    assert "Added 1 new, 2 already known." in status

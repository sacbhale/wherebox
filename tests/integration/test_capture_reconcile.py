"""Reconcile a location against a photo (AC-20, plan 3.6 step 6)."""

import pytest

from app.ai.fake import FakeProvider
from app.capture import reconcile
from app.db.connection import write_tx
from app.inventory import items, locations
from tests.capture_helpers import (
    FIXTURES,
    ai_calls,
    capture_id,
    capture_items,
    capture_row,
    item,
    result,
    run,
)
from tests.inventory_helpers import HX, code_of, open_db, seed_items


def post_reconcile(client, location_id: int):
    path = FIXTURES / "shelf.jpg"
    return client.post(
        "/captures/reconcile",
        headers=HX,
        data={"location_id": str(location_id)},
        files={"photo": (path.name, path.read_bytes(), "image/jpeg")},
    )


@pytest.fixture
def shelf(auth_client, auth_settings) -> dict[str, int]:
    """A Shelf with Tape x2, Glue x1, Screws x10 directly on it, and Nails x5 in a child box."""
    with open_db(auth_settings) as conn:
        shelf_id, _ = locations.create(conn, "Shelf")
        box_id, _ = locations.create(conn, "Box", shelf_id)
        tape, glue, screws, nails = seed_items(
            conn,
            [
                items.ItemData(name="Tape", location_id=shelf_id, quantity=2),
                items.ItemData(name="Glue", location_id=shelf_id, quantity=1),
                items.ItemData(name="Screws", location_id=shelf_id, quantity=10),
                items.ItemData(name="Nails", location_id=box_id, quantity=5),
            ],
        )
    return {
        "shelf": shelf_id,
        "box": box_id,
        "tape": tape,
        "glue": glue,
        "screws": screws,
        "nails": nails,
    }


def proposals(conn) -> list[tuple]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT type, item_id, proposed_quantity, status FROM review_proposals ORDER BY id"
        )
    ]


def item_row(conn, item_id: int):
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def test_reconcile_adds_new_items_and_proposes_the_rest(auth_client, auth_settings, shelf):
    response = post_reconcile(auth_client, shelf["shelf"])
    assert response.status_code == 200
    cid = capture_id(response)
    assert "Reconcile" in response.text

    provider = FakeProvider(
        [
            result(
                [
                    item("Tape", quantity=2, matched_existing_id=shelf["tape"]),  # same count
                    item("Screws", quantity=7, matched_existing_id=shelf["screws"]),  # changed
                    item("Hammer", confidence=0.95, kind="belonging"),  # new, confident
                    item("Odd widget", confidence=0.3),  # new, uncertain
                ]
            )
        ]
    )
    assert run(auth_settings, provider) is True

    request = provider.requests[0]
    assert request.mode == "reconcile"
    # Only live items directly on the shelf (not the child box's Nails).
    assert [(e.id, e.name, e.quantity) for e in request.existing_items] == [
        (shelf["tape"], "Tape", 2),
        (shelf["glue"], "Glue", 1),
        (shelf["screws"], "Screws", 10),
    ]
    assert len(request.images) == 1

    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        new = capture_items(conn, cid)
        photo = conn.execute("SELECT id FROM photos WHERE capture_id = ?", (cid,)).fetchone()
        rows = {key: item_row(conn, shelf[key]) for key in ("tape", "glue", "screws", "nails")}
        props = proposals(conn)
        found = conn.execute(
            "SELECT rowid FROM items_fts WHERE items_fts MATCH 'hammer'"
        ).fetchall()
    assert capture["status"] == "done", capture["error"]

    # New items are added automatically, at the location, with the photo and confidence rule.
    assert [(r["name"], r["location_id"], r["needs_review"]) for r in new] == [
        ("Hammer", shelf["shelf"], 0),
        ("Odd widget", shelf["shelf"], 1),
    ]
    assert {r["photo_id"] for r in new} == {photo["id"]}
    assert [r[0] for r in found] == [new[0]["id"]]  # reindexed in the same transaction

    # Seen items get last_seen_at; nothing is removed or decremented without a tap.
    assert rows["tape"]["last_seen_at"] is not None
    assert rows["screws"]["last_seen_at"] is not None
    assert rows["glue"]["last_seen_at"] is None
    assert [rows[k]["quantity"] for k in ("tape", "glue", "screws", "nails")] == [2, 1, 10, 5]
    assert all(rows[k]["deleted_at"] is None for k in rows)

    assert sorted(props) == sorted(
        [
            ("quantity", shelf["screws"], 7, "open"),
            ("missing", shelf["glue"], None, "open"),
        ]
    )
    with open_db(auth_settings) as conn:
        capture_ids = {r[0] for r in conn.execute("SELECT capture_id FROM review_proposals")}
    assert capture_ids == {cid}


def test_invalid_matched_existing_id_becomes_a_new_item(auth_client, auth_settings, shelf):
    cid = capture_id(post_reconcile(auth_client, shelf["shelf"]))
    provider = FakeProvider(
        [
            result(
                [
                    item("Nails", quantity=3, matched_existing_id=shelf["nails"]),  # other loc
                    item("Ghost", quantity=1, matched_existing_id=99999),  # not an item
                    item("Tape", quantity=2, matched_existing_id=shelf["tape"]),
                    item("Glue", quantity=1, matched_existing_id=shelf["glue"]),
                    item("Screws", quantity=10, matched_existing_id=shelf["screws"]),
                ]
            )
        ]
    )
    run(auth_settings, provider)
    with open_db(auth_settings) as conn:
        new = capture_items(conn, cid)
        nails = item_row(conn, shelf["nails"])
        props = proposals(conn)
    assert [(r["name"], r["quantity"], r["location_id"]) for r in new] == [
        ("Nails", 3, shelf["shelf"]),
        ("Ghost", 1, shelf["shelf"]),
    ]
    assert nails["quantity"] == 5 and nails["location_id"] == shelf["box"]
    assert nails["last_seen_at"] is None
    assert props == []  # everything on the shelf was seen with the same count


def test_duplicate_open_proposal_insert_yields_one_row(auth_client, auth_settings, shelf):
    with open_db(auth_settings) as conn:
        with write_tx(conn):
            assert reconcile.propose(conn, "missing", shelf["glue"], None) is True
            assert reconcile.propose(conn, "quantity", shelf["glue"], None, 4) is False
        assert proposals(conn) == [("missing", shelf["glue"], None, "open")]

        # Once resolved, a new open proposal for the item is allowed again.
        with write_tx(conn):
            conn.execute("UPDATE review_proposals SET status = 'dismissed'")
            assert reconcile.propose(conn, "quantity", shelf["glue"], None, 4) is True
        assert len(proposals(conn)) == 2


def test_two_reconciles_keep_one_open_proposal_per_item(auth_client, auth_settings, shelf):
    seen_tape_only = result([item("Tape", quantity=5, matched_existing_id=shelf["tape"])])
    for _ in range(2):
        post_reconcile(auth_client, shelf["shelf"])
    run(auth_settings, FakeProvider([seen_tape_only]))
    run(auth_settings, FakeProvider([seen_tape_only]))

    with open_db(auth_settings) as conn:
        statuses = [r[0] for r in conn.execute("SELECT status FROM captures ORDER BY id")]
        props = proposals(conn)
        open_per_item = conn.execute(
            "SELECT item_id, count(*) FROM review_proposals WHERE status = 'open' GROUP BY item_id"
        ).fetchall()
    assert statuses == ["done", "done"]
    assert sorted(props) == sorted(
        [
            ("quantity", shelf["tape"], 5, "open"),
            ("missing", shelf["glue"], None, "open"),
            ("missing", shelf["screws"], None, "open"),
        ]
    )
    assert all(count == 1 for _, count in open_per_item)


def test_reconcile_against_a_deleted_location_fails_before_any_provider_call(
    auth_client, auth_settings, shelf
):
    cid = capture_id(post_reconcile(auth_client, shelf["shelf"]))
    with open_db(auth_settings) as conn:
        locations.delete(conn, shelf["shelf"])
    provider = FakeProvider([result([item("Hammer")])])
    assert run(auth_settings, provider) is True

    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        calls = ai_calls(conn)
        props = proposals(conn)
        new = capture_items(conn, cid)
    assert capture["status"] == "failed"
    assert capture["error"] == "location removed"
    assert provider.requests == []
    assert calls == [] and props == [] and new == []

    status = auth_client.get(f"/captures/{cid}/status")
    assert status.status_code == 286 and "location removed" in status.text


def test_reconcile_endpoint_validates_input(auth_client, auth_settings, shelf):
    no_photo = auth_client.post(
        "/captures/reconcile", headers=HX, data={"location_id": str(shelf["shelf"])}
    )
    assert no_photo.status_code == 400
    no_location = auth_client.post(
        "/captures/reconcile",
        headers=HX,
        files={"photo": ("a.jpg", (FIXTURES / "shelf.jpg").read_bytes(), "image/jpeg")},
    )
    assert no_location.status_code == 400
    with open_db(auth_settings) as conn:
        assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0


def test_location_panel_and_reconcile_page_offer_the_reconcile_form(
    auth_client, auth_settings, shelf
):
    panel = auth_client.get("/captures/panel", params={"loc": shelf["shelf"]})
    assert 'id="capture-reconcile"' in panel.text
    assert 'hx-post="/captures/reconcile"' in panel.text
    home_panel = auth_client.get("/captures/panel")
    assert 'id="capture-reconcile"' not in home_panel.text

    with open_db(auth_settings) as conn:
        code = code_of(conn, shelf["shelf"])
    page = auth_client.get(f"/l/{code}/reconcile")
    assert page.status_code == 200
    assert "<h1>Reconcile Shelf</h1>" in page.text
    assert 'id="capture-reconcile-input"' in page.text


def test_item_added_during_the_ai_call_gets_no_missing_proposal(
    auth_client, auth_settings, shelf, monkeypatch
):
    post_reconcile(auth_client, shelf["shelf"])
    seen_all = result(
        [
            item("Tape", quantity=2, matched_existing_id=shelf["tape"]),
            item("Glue", quantity=1, matched_existing_id=shelf["glue"]),
            item("Screws", quantity=10, matched_existing_id=shelf["screws"]),
        ]
    )
    provider = FakeProvider([seen_all])
    extract = provider.extract
    late: list[int] = []

    def extract_after_new_item(request):
        with open_db(auth_settings) as conn:
            (late_id,) = seed_items(
                conn, [items.ItemData(name="Late", location_id=shelf["shelf"], quantity=1)]
            )
        late.append(late_id)
        return extract(request)

    monkeypatch.setattr(provider, "extract", extract_after_new_item)
    run(auth_settings, provider)
    with open_db(auth_settings) as conn:
        props = proposals(conn)
    assert late and props == []  # Late was never sent to the model, so it is not "missing"


def test_seeing_a_missing_item_again_dismisses_its_proposal(auth_client, auth_settings, shelf):
    for _ in range(2):
        post_reconcile(auth_client, shelf["shelf"])
    only_tape = result([item("Tape", quantity=2, matched_existing_id=shelf["tape"])])
    run(auth_settings, FakeProvider([only_tape]))
    with open_db(auth_settings) as conn:
        assert ("missing", shelf["glue"], None, "open") in proposals(conn)

    glue_again = result(
        [
            item("Tape", quantity=2, matched_existing_id=shelf["tape"]),
            item("Glue", quantity=4, matched_existing_id=shelf["glue"]),
            item("Screws", quantity=10, matched_existing_id=shelf["screws"]),
        ]
    )
    run(auth_settings, FakeProvider([glue_again]))
    with open_db(auth_settings) as conn:
        props = proposals(conn)
        resolved = conn.execute(
            "SELECT resolved_at FROM review_proposals WHERE item_id = ? AND type = 'missing'",
            (shelf["glue"],),
        ).fetchone()
    assert ("missing", shelf["glue"], None, "dismissed") in props
    assert ("quantity", shelf["glue"], 4, "open") in props
    assert ("missing", shelf["glue"], None, "open") not in props
    assert resolved["resolved_at"] is not None

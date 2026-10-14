"""Location pages and permanent URLs: AC-2 (tree CRUD over HTTP), AC-5, AC-9, AC-11, AC-12."""

import re
from datetime import timedelta

import pytest

from app.db.connection import write_tx
from app.db.timestamps import utcnow
from app.inventory import items, locations, shortcode
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, logged_in
from tests.inventory_helpers import HX, code_of, item_location, make_chain, open_db, seed_items


def tree_snapshot(settings):
    with open_db(settings) as conn:
        return [
            tuple(r)
            for r in conn.execute(
                "SELECT id, short_code, name, parent_id, deleted_at FROM locations ORDER BY id"
            )
        ]


def create_location(client, name, parent_id=None):
    data = {"name": name} | ({"parent_id": str(parent_id)} if parent_id else {})
    response = client.post("/locations", data=data, follow_redirects=False)
    assert response.status_code == 303
    code = response.headers["location"].removeprefix("/l/")
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{6}", code)
    return code


def location_id(settings, code):
    with open_db(settings) as conn:
        return locations.lookup_code(conn, code)[1]["id"]


def test_depth_8_tree_crud_over_http(auth_client, auth_settings):
    codes, ids = [], []
    parent = None
    for depth in range(1, 9):
        code = create_location(auth_client, f"Level {depth}", parent)
        codes.append(code)
        parent = location_id(auth_settings, code)
        ids.append(parent)
    deepest = auth_client.get(f"/l/{codes[-1]}")
    assert deepest.status_code == 200
    for depth in range(1, 8):
        assert f'<a href="/l/{codes[depth - 1]}">Level {depth}</a>' in deepest.text

    # Rename
    edit = auth_client.get(f"/l/{codes[2]}/edit")
    assert edit.status_code == 200
    assert f'value="{ids[3]}"' not in edit.text  # own subtree is not offered as a parent
    renamed = auth_client.post(
        f"/l/{codes[2]}/rename", data={"name": "Attic"}, follow_redirects=False
    )
    assert renamed.status_code == 303
    assert "Attic" in auth_client.get(f"/l/{codes[-1]}").text

    # Move under its own descendant: 400, tree unchanged
    before = tree_snapshot(auth_settings)
    for target in (ids[2], ids[5], ids[7]):
        bad = auth_client.post(f"/l/{codes[2]}/move", data={"parent_id": str(target)})
        assert bad.status_code == 400
        assert "inside itself" in bad.text
    assert tree_snapshot(auth_settings) == before

    # Valid move to the top level, then back under Level 2
    assert (
        auth_client.post(
            f"/l/{codes[4]}/move", data={"parent_id": ""}, follow_redirects=False
        ).status_code
        == 303
    )
    with open_db(auth_settings) as conn:
        assert locations.get(conn, ids[4])["parent_id"] is None
    auth_client.post(f"/l/{codes[4]}/move", data={"parent_id": str(ids[1])})
    with open_db(auth_settings) as conn:
        assert locations.get(conn, ids[4])["parent_id"] == ids[1]

    assert auth_client.post("/locations", data={"name": "  "}).status_code == 400


def test_delete_confirmation_counts_then_reparents(auth_client, auth_settings):
    with open_db(auth_settings) as conn:
        garage, shelf, box = make_chain(conn, ["Garage", "Shelf", "Box"])
        side, _ = locations.create(conn, "Side", shelf)
        seed_items(conn, [items.ItemData(name=f"thing {n}", location_id=shelf) for n in range(3)])
        codes = {i: code_of(conn, i) for i in (garage, shelf, box, side)}

    confirm = auth_client.get(f"/l/{codes[shelf]}/delete")
    assert confirm.status_code == 200
    counts = re.sub(r"\s+", " ", confirm.text)
    assert "<strong>2</strong> locations inside will move to Garage" in counts
    assert "<strong>3</strong> items directly here will move to Garage" in counts
    assert tree_snapshot(auth_settings)[shelf - 1][4] is None  # GET changed nothing

    done = auth_client.post(f"/l/{codes[shelf]}/delete", follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == f"/l/{codes[garage]}"
    with open_db(auth_settings) as conn:
        assert locations.get(conn, box)["parent_id"] == garage
        assert locations.get(conn, side)["parent_id"] == garage
        assert {i: code_of(conn, i) for i in codes} == codes
        assert (
            conn.execute("SELECT count(*) FROM items WHERE location_id = ?", (garage,)).fetchone()[
                0
            ]
            == 3
        )
    assert auth_client.get(f"/l/{codes[box]}").status_code == 200


def test_inbox_cannot_be_renamed_moved_or_deleted(auth_client, auth_settings):
    with open_db(auth_settings) as conn:
        inbox = locations.inbox(conn)
        (other,) = make_chain(conn, ["Other"])
    code = inbox["short_code"]
    before = tree_snapshot(auth_settings)
    page = auth_client.get(f"/l/{code}")
    assert page.status_code == 200 and f"/l/{code}/delete" not in page.text
    assert auth_client.get(f"/l/{code}/edit").status_code == 400
    assert auth_client.get(f"/l/{code}/delete").status_code == 400
    assert auth_client.post(f"/l/{code}/rename", data={"name": "X"}).status_code == 400
    assert auth_client.post(f"/l/{code}/move", data={"parent_id": str(other)}).status_code == 400
    assert auth_client.post(f"/l/{code}/delete").status_code == 400
    assert tree_snapshot(auth_settings) == before


def test_location_page_lists_children_items_and_subtree(auth_client, auth_settings):
    with open_db(auth_settings) as conn:
        garage, shelf, box, deep = make_chain(conn, ["Garage", "Shelf", "Box", "Deep"])
        seed_items(
            conn,
            [
                items.ItemData(name="Rake", location_id=garage),
                items.ItemData(name="Paint", location_id=shelf),
                items.ItemData(name="Fuses", location_id=deep),
            ],
        )
        code = code_of(conn, garage)
    page = auth_client.get(f"/l/{code}")
    assert page.status_code == 200
    assert "Rake" in page.text and "Paint" not in page.text and "Fuses" not in page.text
    assert ">Shelf</a>" in page.text  # child location
    assert f'href="/l/{code}/capture"' in page.text and f'href="/l/{code}/reconcile"' in page.text
    assert f'href="/l/{code}?sub=1"' in page.text

    subtree = auth_client.get(f"/l/{code}?sub=1")
    for name in ("Rake", "Paint", "Fuses"):
        assert name in subtree.text
    assert "Garage › Shelf › Box › Deep" in subtree.text
    assert box


def test_code_assigned_on_insert_and_survives_rename_move_restart(auth_settings):
    with logged_in(auth_settings) as client:
        code = create_location(client, "Toolbox")
        other = create_location(client, "Other")
        loc = location_id(auth_settings, code)
        client.post(f"/l/{code}/rename", data={"name": "Red toolbox"})
        client.post(f"/l/{code}/move", data={"parent_id": str(location_id(auth_settings, other))})
        assert "Red toolbox" in client.get(f"/l/{code}").text
    # A new app on the same DATA_DIR (restart; migrations re-run).
    with logged_in(auth_settings) as client:
        page = client.get(f"/l/{code}")
        assert page.status_code == 200 and "Red toolbox" in page.text
        assert location_id(auth_settings, code) == loc


def test_displayed_url_uses_public_base_url_not_host(make_settings):
    settings = make_settings(
        household_password=TEST_PASSWORD, public_base_url="https://inv.example.com"
    )
    with logged_in(
        settings,
        base_url="http://192.168.1.20:8000",
        headers={"Origin": "http://192.168.1.20:8000"},
    ) as client:
        with open_db(settings) as conn:
            (loc,) = make_chain(conn, ["Garage"])
            code = code_of(conn, loc)
        page = client.get(f"/l/{code}")
        assert f'value="https://inv.example.com/l/{code}"' in page.text
        assert "192.168.1.20" not in page.text
        assert 'data-copy="#permanent-url"' in page.text
        assert f'src="/l/{code}/qr.svg"' in page.text
        assert f'href="/l/{code}/qr.png?scale=10"' in page.text
        assert f'href="/l/{code}/label"' in page.text


def test_deleted_location_is_410_and_never_another_location(auth_client, auth_settings):
    with open_db(auth_settings) as conn:
        parent, gone = make_chain(conn, ["Parent", "Gone"])
        seed_items(conn, [items.ItemData(name="Orphan", location_id=gone)])
        code = code_of(conn, gone)
        locations.delete(conn, gone)
    for path in (
        f"/l/{code}",
        f"/l/{code.lower()}",
        f"/l/{code}?sub=1",
        f"/l/{code}/label",
        f"/l/{code}/qr.png",
        f"/l/{code}/qr.svg",
        f"/l/{code}/edit",
    ):
        response = auth_client.get(path)
        assert response.status_code == 410, path
        assert "This location was removed" in response.text
        assert "Parent" not in response.text and "Orphan" not in response.text
    assert auth_client.post(f"/l/{code}/rename", data={"name": "x"}).status_code == 410
    assert parent


def test_unknown_code_is_404(auth_client):
    assert auth_client.get("/l/ZZZZZZ").status_code == 404
    assert auth_client.get("/l/ZZZZZZ/qr.png").status_code == 404
    assert auth_client.get("/l/not-a-code-at-all").status_code == 404


@pytest.mark.parametrize(
    "transform",
    [
        str.lower,
        lambda c: c.replace("0", "O").replace("1", "I"),
        lambda c: c.replace("1", "l").lower(),
        lambda c: f"{c[:3]}-{c[3:]}",
        lambda c: f" {c} ",
    ],
)
def test_code_normalization(auth_client, auth_settings, transform):
    code = "K0M1N9"  # fixed so the confusable 0 and 1 are present
    with open_db(auth_settings) as conn, write_tx(conn):
        conn.execute("INSERT INTO locations (short_code, name) VALUES (?, 'Box')", (code,))
    page = auth_client.get(f"/l/{transform(code)}")
    assert page.status_code == 200
    assert f"/l/{code}" in page.text


def test_normalization_maps_confusables():
    assert shortcode.normalize(" ab-o1l-i ") == "AB0111"


# ---------------------------------------------------------------- scan-to-move (AC-6)


@pytest.fixture
def scan_setup(auth_client, auth_settings):
    """An item on Garage › Shelf, and an Attic to scan; returns ids and codes."""
    with open_db(auth_settings) as conn:
        garage, shelf = make_chain(conn, ["Garage", "Shelf"])
        (attic,) = make_chain(conn, ["Attic"])
        (item_id,) = seed_items(conn, [items.ItemData(name="Drill", location_id=shelf)])
        return {
            "item": item_id,
            "shelf": shelf,
            "attic": attic,
            "attic_code": code_of(conn, attic),
            "garage": garage,
        }


def _pending_values(settings):
    with open_db(settings) as conn:
        return [row[0] for row in conn.execute("SELECT pending_move FROM sessions")]


def _start_scan(client, item_id):
    response = client.post(f"/items/{item_id}/pending-move", headers=HX)
    assert response.status_code == 200
    assert "Now scan the container's QR code or tap its NFC tag" in response.text
    assert 'hx-post="/pending-move/cancel"' in response.text
    return response


def test_scan_to_move(auth_client, auth_settings, scan_setup):
    s = scan_setup
    _start_scan(auth_client, s["item"])
    assert item_location(auth_settings, s["item"]) == s["shelf"]  # nothing moves yet

    page = auth_client.get(f"/l/{s['attic_code']}")
    assert page.status_code == 200
    assert f'id="toast-move-{s["item"]}"' in page.text
    assert "Moved <strong>Drill</strong> here" in page.text
    assert f'id="item-{s["item"]}"' in page.text  # listed in its new location
    assert item_location(auth_settings, s["item"]) == s["attic"]
    assert _pending_values(auth_settings) == [None]  # applied once, then cleared
    with open_db(auth_settings) as conn:
        assert "Attic" in auth_client.get("/search?q=drill").text  # FTS reindexed
        assert locations.recent(conn)[0]["id"] == s["attic"]
    assert "toast-move-" not in auth_client.get(f"/l/{s['attic_code']}").text

    # The toast's Undo moves the item back to where it was.
    undo_vals = f'{{"location_id": "{s["shelf"]}", "undo": "1"}}'
    assert undo_vals in page.text
    undo = auth_client.post(
        f"/items/{s['item']}/move", data={"location_id": str(s["shelf"]), "undo": "1"}, headers=HX
    )
    assert undo.status_code == 200 and f'id="item-{s["item"]}"' in undo.text
    assert item_location(auth_settings, s["item"]) == s["shelf"]


def test_scan_to_move_expired_does_nothing(auth_client, auth_settings, scan_setup):
    s = scan_setup
    start = utcnow()
    auth_client.app.state.clock = lambda: start
    _start_scan(auth_client, s["item"])
    auth_client.app.state.clock = lambda: start + timedelta(minutes=11)
    page = auth_client.get(f"/l/{s['attic_code']}")
    assert page.status_code == 200 and "toast-move-" not in page.text
    assert item_location(auth_settings, s["item"]) == s["shelf"]
    assert _pending_values(auth_settings) == [None]  # the expired move was cleared


def test_scan_to_move_still_pending_at_9_minutes(auth_client, auth_settings, scan_setup):
    s = scan_setup
    start = utcnow()
    auth_client.app.state.clock = lambda: start
    _start_scan(auth_client, s["item"])
    auth_client.app.state.clock = lambda: start + timedelta(minutes=9)
    assert "toast-move-" in auth_client.get(f"/l/{s['attic_code']}").text
    assert item_location(auth_settings, s["item"]) == s["attic"]


def test_scan_to_move_cancel(auth_client, auth_settings, scan_setup):
    s = scan_setup
    _start_scan(auth_client, s["item"])
    cancel = auth_client.post("/pending-move/cancel", headers=HX)
    assert cancel.status_code == 200 and cancel.text == ""
    assert "toast-move-" not in auth_client.get(f"/l/{s['attic_code']}").text
    assert item_location(auth_settings, s["item"]) == s["shelf"]


def test_scan_to_move_removed_or_unknown_location_keeps_it_pending(
    auth_client, auth_settings, scan_setup
):
    s = scan_setup
    with open_db(auth_settings) as conn:
        (gone,) = make_chain(conn, ["Gone"])
        gone_code = code_of(conn, gone)
        locations.delete(conn, gone)
    _start_scan(auth_client, s["item"])

    removed = auth_client.get(f"/l/{gone_code}")
    assert removed.status_code == 410
    assert "Your move is still pending" in removed.text
    assert auth_client.get("/l/ZZZZZZ").status_code == 404
    assert item_location(auth_settings, s["item"]) == s["shelf"]
    assert _pending_values(auth_settings) != [None]

    # Scanning a live tag afterwards completes it.
    assert "toast-move-" in auth_client.get(f"/l/{s['attic_code']}").text
    assert item_location(auth_settings, s["item"]) == s["attic"]
    # Without a pending move the 410 page does not mention one.
    assert "still pending" not in auth_client.get(f"/l/{gone_code}").text


def test_scan_to_move_logged_out_scan_applies_after_login(auth_client, auth_settings, scan_setup):
    s = scan_setup
    _start_scan(auth_client, s["item"])
    # The session expires while the move is pending (the browser still sends the cookie).
    with open_db(auth_settings) as conn, write_tx(conn):
        conn.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00.000000Z'")

    scan = auth_client.get(f"/l/{s['attic_code']}", follow_redirects=False)
    assert scan.status_code == 303
    assert scan.headers["location"] == f"/login?next=%2Fl%2F{s['attic_code']}"
    assert item_location(auth_settings, s["item"]) == s["shelf"]

    login = auth_client.post(
        "/login",
        data={
            "username": TEST_USERNAME,
            "password": TEST_PASSWORD,
            "next": f"/l/{s['attic_code']}",
        },
        follow_redirects=False,
    )
    assert login.status_code == 303 and login.headers["location"] == f"/l/{s['attic_code']}"
    page = auth_client.get(login.headers["location"])
    assert page.status_code == 200 and f'id="toast-move-{s["item"]}"' in page.text
    assert item_location(auth_settings, s["item"]) == s["attic"]
    assert _pending_values(auth_settings) == [None]  # the old session row was replaced


def test_expired_session_without_pending_move_is_deleted(auth_client, auth_settings):
    with open_db(auth_settings) as conn, write_tx(conn):
        conn.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00.000000Z'")
    assert auth_client.get("/", follow_redirects=False).status_code == 303
    assert _pending_values(auth_settings) == []

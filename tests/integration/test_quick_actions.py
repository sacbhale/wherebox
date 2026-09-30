"""AC-6 quick actions: +1/-1 inline, the ⋯ action sheet (Used up, Move to recent/parent/
siblings, location search, Scan destination tag, Edit) and the undo toasts. Every action
after opening the sheet is one hx-post, so Used up and Move take 2 taps."""

import re

import pytest

from app.db.connection import write_tx
from app.inventory import items, locations
from tests.inventory_helpers import HX, code_of, item_location, make_chain, open_db, seed_items


@pytest.fixture
def tree(auth_client, auth_settings):
    """Home › Room › {Current, Side 1, Side 2}, roots R1..R6; one item in Current.

    last_used_at is set explicitly (oldest R1 ... newest Current) so the recent order is
    deterministic: recent excluding Current = Side 2, R6, R5, R4, R3."""
    with open_db(auth_settings) as conn:
        home, room, current = make_chain(conn, ["Home", "Room", "Current"])
        side1, _ = locations.create(conn, "Side 1", room)
        side2, _ = locations.create(conn, "Side 2", room)
        roots = [locations.create(conn, f"R{n}")[0] for n in range(1, 7)]
        (item_id,) = seed_items(conn, [items.ItemData(name="Tape", location_id=current)])
        order = [*roots, side2, current]
        with write_tx(conn):
            conn.execute("UPDATE locations SET last_used_at = NULL")
            for minute, loc in enumerate(order):
                conn.execute(
                    "UPDATE locations SET last_used_at = ? WHERE id = ?",
                    (f"2026-01-01T00:{minute:02d}:00.000Z", loc),
                )
        return {
            "home": home,
            "room": room,
            "current": current,
            "side1": side1,
            "side2": side2,
            "roots": roots,
            "item": item_id,
            "code": code_of(conn, current),
        }


def _target_ids(html):
    return [int(v) for v in re.findall(r'data-location-id="(\d+)"', html)]


def test_action_sheet_lists_recent_parent_siblings_without_current(auth_client, tree):
    sheet = auth_client.get(f"/items/{tree['item']}/actions")
    assert sheet.status_code == 200
    r = tree["roots"]
    # Recent 5 (excluding Current), then the parent, then siblings; Side 2 is deduplicated,
    # Current, the grandparent and R1/R2 (not recent enough) are left out.
    assert _target_ids(sheet.text) == [
        tree["side2"],
        r[5],
        r[4],
        r[3],
        r[2],
        tree["room"],
        tree["side1"],
    ]
    item = tree["item"]
    assert 'id="sheet-used-up"' in sheet.text and f'hx-post="/items/{item}/used-up"' in sheet.text
    assert f'hx-post="/items/{item}/pending-move"' in sheet.text  # Scan destination tag
    assert f'href="/items/{item}/edit"' in sheet.text
    assert f'hx-get="/items/{item}/move-targets"' in sheet.text
    assert auth_client.get("/items/99999/actions").status_code == 404


def test_action_sheet_for_root_location_offers_other_roots(auth_client, auth_settings, tree):
    with open_db(auth_settings) as conn:
        (item_id,) = seed_items(conn, [items.ItemData(name="Box", location_id=tree["roots"][0])])
        inbox = locations.inbox(conn)["id"]
    ids = _target_ids(auth_client.get(f"/items/{item_id}/actions").text)
    assert tree["roots"][0] not in ids  # never the current location
    assert {inbox, tree["home"], *tree["roots"][1:]} <= set(ids)  # siblings of a root
    assert len(ids) == len(set(ids))


def test_move_search_returns_tap_to_move_results(auth_client, tree):
    found = auth_client.get(f"/items/{tree['item']}/move-targets", params={"q": "room"})
    assert found.status_code == 200
    # Room and its children match by path; Current (where the item is) is excluded.
    assert set(_target_ids(found.text)) == {tree["room"], tree["side1"], tree["side2"]}
    assert "Home › Room › Side 1" in found.text
    assert f'hx-post="/items/{tree["item"]}/move"' in found.text
    assert _target_ids(auth_client.get(f"/items/{tree['item']}/move-targets?q=").text) == []
    assert (
        "No matching locations"
        in auth_client.get(f"/items/{tree['item']}/move-targets?q=nowhere").text
    )


def test_move_is_one_post_with_undo_toast(auth_client, auth_settings, tree):
    item = tree["item"]
    moved = auth_client.post(
        f"/items/{item}/move", data={"location_id": str(tree["side1"])}, headers=HX
    )
    assert moved.status_code == 200
    assert f'id="item-{item}"' in moved.text and 'role="status"' in moved.text
    assert "Moved Tape to" in moved.text and "Home › Room › Side 1</a>" in moved.text
    assert f'{{"location_id": "{tree["current"]}", "undo": "1"}}' in moved.text
    assert item_location(auth_settings, item) == tree["side1"]

    undo = auth_client.post(
        f"/items/{item}/move", data={"location_id": str(tree["current"]), "undo": "1"}, headers=HX
    )
    assert undo.status_code == 200
    assert f'id="more-{item}"' in undo.text  # the normal row is back
    assert item_location(auth_settings, item) == tree["current"]


def test_used_up_is_one_post_and_undo_restores(auth_client, auth_settings, tree):
    item = tree["item"]
    used = auth_client.post(f"/items/{item}/used-up", headers=HX)
    assert used.status_code == 200 and f'hx-post="/items/{item}/restore"' in used.text
    with open_db(auth_settings) as conn:
        assert items.get(conn, item) is None
    restored = auth_client.post(f"/items/{item}/restore", headers=HX)
    assert restored.status_code == 200 and f'id="more-{item}"' in restored.text


def test_minus_floors_at_zero_keeps_item_and_is_disabled(auth_client, auth_settings, tree):
    item = tree["item"]
    row = auth_client.post(f"/items/{item}/quantity", data={"delta": "-1"}, headers=HX).text
    assert re.search(
        rf'<button[^>]*id="minus-{item}"[^>]*\bdisabled\b[^>]*>\u22121</button>', row, re.S
    )
    assert not re.search(rf'<button[^>]*id="plus-{item}"[^>]*\bdisabled\b', row, re.S)
    again = auth_client.post(f"/items/{item}/quantity", data={"delta": "-1"}, headers=HX)
    assert again.status_code == 200 and 'aria-label="Quantity">0</span>' in again.text
    with open_db(auth_settings) as conn:
        assert items.get(conn, item)["quantity"] == 0
    plus = auth_client.post(f"/items/{item}/quantity", data={"delta": "1"}, headers=HX).text
    assert not re.search(rf'<button[^>]*id="minus-{item}"[^>]*\bdisabled\b', plus, re.S)


def test_scan_hint_requires_htmx_and_a_live_item(auth_client, tree):
    assert auth_client.post(f"/items/{tree['item']}/pending-move").status_code == 403
    assert auth_client.post("/items/99999/pending-move", headers=HX).status_code == 404
    assert auth_client.post("/pending-move/cancel").status_code == 403


@pytest.mark.parametrize("where", ["location", "search_page", "search_partial"])
def test_rows_offer_one_tap_quick_actions_everywhere(auth_client, tree, where):
    item = tree["item"]
    if where == "location":
        html = auth_client.get(f"/l/{tree['code']}").text
    elif where == "search_page":
        html = auth_client.get("/search?q=tape").text
    else:
        html = auth_client.get("/search?q=tape", headers=HX).text  # the home page's live search
    assert re.search(rf'id="plus-{item}"[^>]*hx-post="/items/{item}/quantity"', html, re.S)
    assert re.search(rf'id="minus-{item}"[^>]*hx-post="/items/{item}/quantity"', html, re.S)
    assert re.search(rf'id="more-{item}"[^>]*hx-get="/items/{item}/actions"', html, re.S)
    if where != "search_partial":
        assert '<div id="sheet"></div>' in html

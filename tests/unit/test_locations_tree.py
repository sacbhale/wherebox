"""Location tree repository: paths, subtrees, cycle guard, delete re-parenting, Inbox."""

import pytest

from app.inventory import items, locations
from app.inventory.search import PATH_SEP
from tests.inventory_helpers import code_of, make_chain, seed_items

NAMES = [f"L{depth}" for depth in range(1, 9)]  # depth 8


def snapshot(conn):
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT id, short_code, name, parent_id, deleted_at FROM locations ORDER BY id"
        )
    ]


def test_depth_8_paths_and_subtrees(conn):
    ids = make_chain(conn, NAMES)
    assert locations.path(conn, ids[-1]) == PATH_SEP.join(NAMES)
    assert [r["id"] for r in locations.ancestors(conn, ids[-1])] == ids
    assert sorted(locations.subtree_ids(conn, ids[0])) == sorted(ids)
    assert locations.subtree_ids(conn, ids[-1]) == [ids[-1]]
    assert [r["id"] for r in locations.children(conn, ids[3])] == [ids[4]]


def test_every_code_is_six_crockford_chars(conn):
    ids = make_chain(conn, NAMES)
    for location_id in ids:
        code = code_of(conn, location_id)
        assert len(code) == 6 and set(code) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_rename_and_move(conn):
    ids = make_chain(conn, NAMES)
    other, _ = locations.create(conn, "Other")
    locations.rename(conn, ids[2], "  Renamed   box ")
    assert locations.get(conn, ids[2])["name"] == "Renamed box"
    locations.move(conn, ids[4], other)
    assert locations.path(conn, ids[-1]) == PATH_SEP.join(["Other", *NAMES[4:]])
    locations.move(conn, ids[4], None)
    assert locations.get(conn, ids[4])["parent_id"] is None


@pytest.mark.parametrize("target_index", [0, 3, 7])
def test_move_into_own_subtree_is_rejected_and_tree_unchanged(conn, target_index):
    ids = make_chain(conn, NAMES)
    before = snapshot(conn)
    with pytest.raises(locations.LocationError):
        locations.move(conn, ids[0], ids[target_index])
    assert snapshot(conn) == before
    assert not conn.in_transaction


def test_delete_reparents_children_and_items_and_keeps_codes(conn):
    ids = make_chain(conn, NAMES)
    mid = ids[3]
    sibling, _ = locations.create(conn, "Sibling", mid)
    item_ids = seed_items(
        conn,
        [
            items.ItemData(name="direct", location_id=mid),
            items.ItemData(name="deeper", location_id=ids[5]),
        ],
    )
    codes_before = {i: code_of(conn, i) for i in (*ids, sibling)}
    assert locations.delete_counts(conn, mid) == locations.DeleteCounts(2, 1)
    assert locations.delete(conn, mid) == locations.DeleteCounts(2, 1)
    assert locations.get(conn, mid) is None
    assert locations.get(conn, ids[4])["parent_id"] == ids[2]
    assert locations.get(conn, sibling)["parent_id"] == ids[2]
    assert items.get(conn, item_ids[0])["location_id"] == ids[2]
    assert items.get(conn, item_ids[1])["location_id"] == ids[5]
    assert {i: code_of(conn, i) for i in codes_before} == codes_before
    assert locations.lookup_code(conn, codes_before[mid])[0] == "deleted"


def test_delete_root_sends_items_to_inbox_and_children_to_top(conn):
    root, child = make_chain(conn, ["Root", "Child"])
    (item_id,) = seed_items(conn, [items.ItemData(name="thing", location_id=root)])
    locations.delete(conn, root)
    assert items.get(conn, item_id)["location_id"] == locations.inbox(conn)["id"]
    assert locations.get(conn, child)["parent_id"] is None


def test_used_up_items_follow_reparenting(conn):
    root, box = make_chain(conn, ["Root", "Box"])
    (item_id,) = seed_items(conn, [items.ItemData(name="thing", location_id=box)])
    items.use_up(conn, item_id)
    locations.delete(conn, box)
    assert items.undo_use_up(conn, item_id)
    assert items.get(conn, item_id)["location_id"] == root


def test_inbox_is_protected(conn):
    inbox = locations.inbox(conn)
    other, _ = locations.create(conn, "Other")
    before = snapshot(conn)
    for action in (
        lambda: locations.rename(conn, inbox["id"], "Mailbox"),
        lambda: locations.move(conn, inbox["id"], other),
        lambda: locations.delete(conn, inbox["id"]),
    ):
        with pytest.raises(locations.LocationError, match="Inbox"):
            action()
    assert snapshot(conn) == before


def test_lookup_code_normalizes(conn):
    (loc,) = make_chain(conn, ["Box"])
    code = code_of(conn, loc)
    messy = code.lower().replace("0", "o").replace("1", "l")
    messy = messy[:3] + "-" + messy[3:]
    status, row = locations.lookup_code(conn, messy)
    assert (status, row["id"]) == ("live", loc)
    assert locations.lookup_code(conn, "ZZZZZZ") == ("unknown", None)


def test_invalid_names_and_parents(conn):
    with pytest.raises(locations.LocationError):
        locations.create(conn, "   ")
    with pytest.raises(locations.LocationError):
        locations.create(conn, "x", parent_id=9999)
    (loc,) = make_chain(conn, ["Box"])
    locations.delete(conn, loc)
    with pytest.raises(locations.LocationError):
        locations.create(conn, "x", parent_id=loc)


def test_recent_locations(conn):
    a, b = make_chain(conn, ["A", "B"])
    (item_id,) = seed_items(conn, [items.ItemData(name="x", location_id=a)])
    items.move_item(conn, item_id, b)
    assert [r["id"] for r in locations.recent(conn)][:2] == [b, a]

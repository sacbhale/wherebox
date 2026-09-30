"""The home page location tree: ``locations.tree`` and its rendering."""

import re

from app.inventory import items, locations
from tests.inventory_helpers import code_of, make_chain, open_db


def shape(nodes):
    return [(n.name, n.item_count, n.sub_count, shape(n.children)) for n in nodes]


def test_tree_nesting_order_and_counts(conn):
    garage, shelf2, blue = make_chain(conn, ["Garage", "Shelf 2", "Blue box"])
    locations.create(conn, "shelf 1", garage)
    locations.create(conn, "Attic")
    locations.create(conn, "kitchen")
    for name in ("Tape", "Glue"):
        items.create(conn, items.ItemData(name=name, location_id=blue))
    items.create(conn, items.ItemData(name="Drill", location_id=garage))
    used = items.create(conn, items.ItemData(name="Old", location_id=garage))
    items.use_up(conn, used)

    assert shape(locations.tree(conn)) == [
        ("Inbox", 0, 0, []),
        ("Attic", 0, 0, []),
        (
            "Garage",
            1,
            2,
            [("shelf 1", 0, 0, []), ("Shelf 2", 0, 1, [("Blue box", 2, 0, [])])],
        ),
        ("kitchen", 0, 0, []),
    ]
    # Same sibling order as children().
    roots = locations.tree(conn)
    assert [n.id for n in roots] == [r["id"] for r in locations.children(conn, None)]
    garage_node = roots[2]
    assert [n.id for n in garage_node.children] == [
        r["id"] for r in locations.children(conn, garage)
    ]
    blue_node = garage_node.children[1].children[0]
    assert (blue_node.id, blue_node.short_code) == (blue, code_of(conn, blue))
    assert garage_node.children[1].id == shelf2
    assert roots[0].is_system and not garage_node.is_system


def test_deleted_locations_are_excluded_and_children_reparented(conn):
    garage, shelf, box = make_chain(conn, ["Garage", "Shelf", "Box"])
    locations.delete(conn, shelf)
    (_, garage_node) = locations.tree(conn)
    assert [n.id for n in garage_node.children] == [box]
    assert garage_node.sub_count == 1
    locations.delete(conn, garage)
    assert [n.name for n in locations.tree(conn)] == ["Inbox", "Box"]


def count_selects(conn, fn):
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        fn()
    finally:
        conn.set_trace_callback(None)
    return sum(1 for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH")))


def test_tree_uses_one_query_regardless_of_size(conn):
    make_chain(conn, ["A", "B"])
    small = count_selects(conn, lambda: locations.tree(conn))
    ids = make_chain(conn, [f"Level {n}" for n in range(1, 9)])
    for parent in ids:
        for n in range(3):
            leaf, _ = locations.create(conn, f"Leaf {n}", parent)
            items.create(conn, items.ItemData(name="Thing", location_id=leaf))
    large = count_selects(conn, lambda: locations.tree(conn))
    assert small == large == 1


def test_home_renders_nested_tree(auth_client, auth_settings):
    with open_db(auth_settings) as conn:
        garage, shelf2, blue, lid = make_chain(conn, ["Garage", "Shelf 2", "Blue box", "Lid"])
        attic, _ = locations.create(conn, "Attic")
        blue_code = code_of(conn, blue)
        lid_code = code_of(conn, lid)
        inbox_id = locations.inbox(conn)["id"]
    html = auth_client.get("/").text

    assert f'<a class="tree-link" href="/l/{blue_code}">Blue box</a>' in html
    assert f'href="/l/{lid_code}"' in html  # depth 4
    # Chevrons only on nodes with sub-locations.
    for parent in (garage, shelf2, blue):
        assert f'id="toggle-{parent}"' in html
    for leaf in (lid, attic, inbox_id):
        assert f'id="toggle-{leaf}"' not in html
    assert html.count('class="tree-toggle"') == 3
    assert "1 sub-location<" in html
    # Inbox first, then roots by name; nested inside its parent's <details>.
    names = re.findall(r'class="tree-link" href="/l/\w+">([^<]+)</a>', html)
    assert names == ["Inbox", "Attic", "Garage", "Shelf 2", "Blue box", "Lid"]
    garage_details = html.index(f'<details class="tree-branch" data-location-id="{garage}"')
    assert garage_details < html.index(f'href="/l/{blue_code}"')
    # The Add location form is still there.
    assert 'action="/locations"' in html and "Add location" in html


def test_deep_chain_is_capped_and_home_still_renders(auth_client, auth_settings):
    # A chain far past the old ~250-level crash point: the tree stops nesting at
    # TREE_MAX_DEPTH, and the deepest shown node points to its page for the rest.
    names = [f"L{n}" for n in range(1, 301)]
    with open_db(auth_settings) as conn:
        ids = make_chain(conn, names)
        roots = locations.tree(conn)
    node, depth = roots[1], 1
    while node.children:
        (node,) = node.children
        depth += 1
    assert depth == locations.TREE_MAX_DEPTH
    assert node.id == ids[depth - 1] and node.sub_count == 1

    response = auth_client.get("/")
    assert response.status_code == 200
    html = response.text
    assert f'href="/l/{code_of_settings(auth_settings, ids[depth - 1])}"' in html
    assert f'id="toggle-{ids[depth - 1]}"' not in html  # nothing to expand in place
    assert f'id="toggle-{ids[depth - 2]}"' in html
    assert "L13<" not in html


def code_of_settings(settings, location_id):
    with open_db(settings) as conn:
        return code_of(conn, location_id)


def test_corrupt_parent_links_do_not_hang_or_nest_forever(conn):
    # Only possible through manual DB edits: the move guard prevents both.
    selfish, _ = locations.create(conn, "Selfish")
    a, b = make_chain(conn, ["A", "B"])
    conn.execute("UPDATE locations SET parent_id = id WHERE id = ?", (selfish,))
    conn.execute("UPDATE locations SET parent_id = ? WHERE id = ?", (b, a))  # A <-> B cycle
    roots = locations.tree(conn)
    assert [(n.name, n.children) for n in roots] == [("Inbox", []), ("Selfish", [])]


def test_name_links_sit_outside_the_toggle(auth_client, auth_settings):
    with open_db(auth_settings) as conn:
        make_chain(conn, ["Garage", "Shelf", "Box"])
    html = auth_client.get("/").text
    summaries = re.findall(r"<summary.*?</summary>", html, re.S)
    assert summaries and not any("tree-link" in s for s in summaries)

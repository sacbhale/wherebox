"""AC-3 search (5,000 items, path display, p95, hostile input), FTS consistency, reindex perf."""

import statistics
import time

import pytest

from app.inventory import categories, items, locations, search
from tests.inventory_helpers import (
    HX,
    expected_fts_rows,
    fts_rows,
    make_chain,
    open_db,
    seed_items,
)

WORDS = ["hammer", "screws", "tape", "glue", "rope", "jar", "towel", "lamp", "cable", "box"]


@pytest.fixture
def seeded(auth_client, auth_settings):
    """5,000 items across a small tree, plus "AA batteries" in Garage › Shelf 2 › Blue box."""
    with open_db(auth_settings) as conn:
        garage, _shelf, box = make_chain(conn, ["Garage", "Shelf 2", "Blue box"])
        kitchen, pantry = make_chain(conn, ["Kitchen", "Pantry"])
        homes = [garage, box, kitchen, pantry]
        rows = [
            items.ItemData(
                name=f"{WORDS[n % 10]} {WORDS[(n // 10) % 10]} {n}",
                location_id=homes[n % 4],
                category=["Tools", "Food", None][n % 3],
                notes=f"note {n}",
            )
            for n in range(4_999)
        ]
        rows.append(items.ItemData(name="AA batteries", location_id=box, category="Electronics"))
        ids = seed_items(conn, rows)
        assert conn.execute("SELECT count(*) FROM items_fts").fetchone()[0] == 5_000
    return {"battery_id": ids[-1]}


def test_search_finds_prefix_with_full_path(auth_client, seeded):
    page = auth_client.get("/search?q=batt")
    assert page.status_code == 200
    assert "AA batteries" in page.text
    assert "Garage › Shelf 2 › Blue box" in page.text
    assert 'hx-trigger="input changed delay:250ms, search"' in page.text
    assert "<html" in page.text

    partial = auth_client.get("/search?q=batt", headers=HX)
    assert partial.status_code == 200
    assert "<html" not in partial.text
    assert f'id="item-{seeded["battery_id"]}"' in partial.text
    assert "Garage › Shelf 2 › Blue box" in partial.text


def test_search_p95_under_150ms(auth_client, seeded, record_property):
    auth_client.get("/search?q=batt", headers=HX)  # warm-up
    timings = []
    for n in range(40):
        q = ["batt", "hammer", "tape glue", "shelf", "box 1"][n % 5]
        start = time.perf_counter()
        response = auth_client.get("/search", params={"q": q}, headers=HX)
        timings.append((time.perf_counter() - start) * 1000)
        assert response.status_code == 200
    p95 = statistics.quantiles(timings, n=20)[-1]
    record_property("search_p95_ms", round(p95, 2))
    print(f"\nsearch p95 over {len(timings)} requests: {p95:.1f} ms (client-measured)")
    assert p95 < 150


def test_results_capped_at_50(auth_client, seeded):
    with open_db(auth_client.app.state.settings) as conn:
        assert len(search.search(conn, "hammer")) == 50
    assert auth_client.get("/search?q=hammer", headers=HX).text.count('class="item"') == 50


@pytest.mark.parametrize("q", ['"', "AND", "*(", "OR NOT", "NEAR(", '"batt', "col:x", "^", "\\"])
def test_hostile_input_returns_200(auth_client, seeded, q):
    assert auth_client.get("/search", params={"q": q}).status_code == 200
    assert auth_client.get("/search", params={"q": q}, headers=HX).status_code == 200


def test_bad_filter_values_are_400(auth_client):
    assert auth_client.get("/search?expiring_days=soon").status_code == 400
    assert auth_client.get("/search?kind=gadget").status_code == 400
    assert auth_client.get("/search?loc=-1").status_code == 400


def test_home_has_live_search_box(auth_client):
    home = auth_client.get("/")
    assert home.status_code == 200
    assert 'hx-get="/search"' in home.text and 'hx-trigger="input changed delay:250ms' in home.text
    assert "/static/vendor/htmx.min.js" in home.text
    assert "Inbox" in home.text
    assert auth_client.get("/static/vendor/htmx.min.js").status_code == 200


def test_fts_consistency(conn):
    def check(step):
        assert fts_rows(conn) == expected_fts_rows(conn), f"items_fts stale after {step}"

    garage, shelf, box = make_chain(conn, ["Garage", "Shelf 2", "Blue box"])
    kitchen, _ = make_chain(conn, ["Kitchen", "Drawer"])
    ids = seed_items(
        conn,
        [
            items.ItemData(name="AA batteries", location_id=box, category="Electronics", notes="x"),
            items.ItemData(name="Tape", location_id=shelf, category="Supplies"),
            items.ItemData(name="Spoon", location_id=kitchen, category="Kitchenware"),
            items.ItemData(name="Fuse", location_id=box, category="electrical"),
        ],
    )
    check("seed")

    items.update(
        conn,
        ids[0],
        items.ItemData(
            name="AAA batteries", location_id=box, category="Power", notes="rechargeable"
        ),
    )
    check("item edit")

    locations.rename(conn, shelf, "Shelf Two")
    check("location rename")

    locations.move(conn, shelf, kitchen)
    check("subtree move")

    power = conn.execute("SELECT id FROM categories WHERE name = 'Power'").fetchone()[0]
    categories.rename(conn, power, "Batteries")
    check("category rename")

    electrical = conn.execute("SELECT id FROM categories WHERE name = 'electrical'").fetchone()[0]
    categories.merge(conn, electrical, power)
    check("category merge")
    assert items.get(conn, ids[3])["category_name"] == "Batteries"

    items.use_up(conn, ids[1])
    check("used up")
    assert ids[1] not in [r[0] for r in fts_rows(conn)]

    items.undo_use_up(conn, ids[1])
    check("used up undo")

    items.move_item(conn, ids[2], box)
    check("item move")

    locations.delete(conn, shelf)
    check("location delete")
    assert garage  # still live


def test_subtree_reindex_perf(conn, record_property):
    top, mid = make_chain(conn, ["Top", "Mid"])
    parents = [mid]
    for depth in range(3):
        parents.append(locations.create(conn, f"Level {depth}", parents[-1])[0])
    seed_items(
        conn,
        [
            items.ItemData(name=f"thing {n}", location_id=parents[n % len(parents)])
            for n in range(2_000)
        ],
    )
    other, _ = locations.create(conn, "Other")
    start = time.perf_counter()
    locations.move(conn, mid, other)
    elapsed_ms = (time.perf_counter() - start) * 1000
    record_property("subtree_move_ms", round(elapsed_ms, 2))
    print(f"\nmoving a 2,000-item subtree incl. reindex: {elapsed_ms:.1f} ms")
    assert elapsed_ms < 300
    paths = {r[0] for r in conn.execute("SELECT DISTINCT location_path FROM items_fts")}
    assert all(p.startswith("Other › Mid") for p in paths)
    assert top

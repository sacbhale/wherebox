"""AC-4: each filter alone and all five combined return exactly the expected ids."""

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.db.connection import write_tx
from app.inventory import items
from tests.inventory_helpers import HX, make_chain, open_db, seed_items


@pytest.fixture
def world(auth_client, auth_settings):
    today = datetime.now(ZoneInfo(auth_settings.tz)).date()
    soon = (today + timedelta(days=3)).isoformat()
    later = (today + timedelta(days=30)).isoformat()
    past = (today - timedelta(days=1)).isoformat()
    with open_db(auth_settings) as conn:
        house, floor, room, cupboard = make_chain(conn, ["House", "Floor", "Room", "Cupboard"])
        (shed,) = make_chain(conn, ["Shed"])
        spec = {
            # name: (location, category, kind, needs_review, expiry)
            "milk": (house, "Food", "consumable", False, soon),
            "deep soup": (cupboard, "Food", "consumable", True, soon),  # depth 4
            "old soup": (room, "Food", "consumable", True, past),  # depth 3
            "far soup": (room, "Food", "consumable", True, later),
            "drill": (floor, "Tools", "belonging", True, None),
            "shed soup": (shed, "Food", "consumable", True, soon),
            "cupboard cup": (cupboard, "Dishes", "belonging", False, None),
        }
        rows = [
            items.ItemData(name=name, location_id=loc, category=cat, kind=kind, expiry_date=exp)
            for name, (loc, cat, kind, _review, exp) in spec.items()
        ]
        ids = dict(zip(spec, seed_items(conn, rows), strict=True))
        with write_tx(conn):
            for name, (*_, review, _exp) in spec.items():
                if review:
                    conn.execute("UPDATE items SET needs_review = 1 WHERE id = ?", (ids[name],))
        cats = {r["name"]: r["id"] for r in conn.execute("SELECT id, name FROM categories")}
    return {
        "ids": ids,
        "loc": {"house": house, "floor": floor, "room": room, "shed": shed},
        "cat": cats,
    }


def found(client, **params):
    response = client.get("/search", params=params, headers=HX)
    assert response.status_code == 200
    return {int(m) for m in re.findall(r'id="item-(\d+)"', response.text)}


def names(world, *wanted):
    return {world["ids"][n] for n in wanted}


def test_location_only_direct(auth_client, world):
    assert found(auth_client, loc=world["loc"]["room"]) == names(world, "old soup", "far soup")


def test_location_subtree_includes_depth_3_and_deeper(auth_client, world):
    assert found(auth_client, loc=world["loc"]["floor"], sub=1) == names(
        world, "drill", "old soup", "far soup", "deep soup", "cupboard cup"
    )
    assert found(auth_client, loc=world["loc"]["house"], sub=1) == names(
        world, "milk", "drill", "old soup", "far soup", "deep soup", "cupboard cup"
    )


def test_category(auth_client, world):
    assert found(auth_client, category=world["cat"]["Tools"]) == names(world, "drill")


def test_kind(auth_client, world):
    assert found(auth_client, kind="belonging") == names(world, "drill", "cupboard cup")


def test_review(auth_client, world):
    assert found(auth_client, review=1) == names(
        world, "deep soup", "old soup", "far soup", "drill", "shed soup"
    )


def test_expiring_days(auth_client, world):
    assert found(auth_client, expiring_days=7) == names(
        world, "milk", "deep soup", "old soup", "shed soup"
    )
    assert found(auth_client, expiring_days=0) == names(world, "old soup")


def test_all_five_combined(auth_client, world):
    params = {
        "loc": world["loc"]["floor"],
        "sub": 1,
        "category": world["cat"]["Food"],
        "kind": "consumable",
        "review": 1,
        "expiring_days": 7,
    }
    assert found(auth_client, **params) == names(world, "deep soup", "old soup")
    assert found(auth_client, q="deep", **params) == names(world, "deep soup")


def test_empty_filter_fields_are_ignored(auth_client, world):
    """The filter form submits blank fields; they mean "any"."""
    assert found(auth_client, q="soup", loc="", category="", kind="", expiring_days="") == names(
        world, "deep soup", "old soup", "far soup", "shed soup"
    )


def test_full_page_filter_form_keeps_selection(auth_client, world):
    page = auth_client.get("/search", params={"kind": "belonging", "review": 1})
    assert '<option value="belonging" selected>' in page.text
    assert 'name="review" value="1" checked' in page.text
    assert "drill" in page.text

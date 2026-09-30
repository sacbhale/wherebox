"""AC-1 / AC-8 manual item form, plus the quick-action endpoints (+1/-1, Used up, undo, move)."""

import pytest
from fastapi.testclient import TestClient

from app.inventory import items, locations
from tests.conftest import TEST_PASSWORD, logged_in
from tests.inventory_helpers import HX, code_of, make_chain, open_db

FULL_FORM = {
    "name": "Cordless drill",
    "quantity": "3",
    "kind": "belonging",
    "category": "Tools",
    "expiry_date": "2031-02-03",
    "purchase_date": "2024-05-06",
    "price": "129.99",
    "notes": "18V, two batteries\nin the orange case",
}


@pytest.fixture
def garage(auth_client, auth_settings):
    with open_db(auth_settings) as conn:
        garage, shelf = make_chain(conn, ["Garage", "Shelf 2"])
        return {"garage": garage, "shelf": shelf, "code": code_of(conn, shelf)}


def _only_item(settings):
    with open_db(settings) as conn:
        (row,) = conn.execute("SELECT * FROM items").fetchall()
        return dict(row), conn.execute(
            "SELECT name FROM categories WHERE id = ?", (row["category_id"],)
        ).fetchone()


def test_manual_form_round_trips_every_field(auth_client, auth_settings, garage):
    page = auth_client.get(f"/items/new?loc={garage['shelf']}")
    assert page.status_code == 200
    for field in (*FULL_FORM, "location_id"):
        assert f'name="{field}"' in page.text
    assert f'<option value="{garage["shelf"]}" selected>Garage › Shelf 2</option>' in page.text

    response = auth_client.post(
        "/items/new",
        data={**FULL_FORM, "location_id": str(garage["shelf"])},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/l/{garage['code']}"

    row, category = _only_item(auth_settings)
    assert category["name"] == "Tools"
    assert {
        k: row[k]
        for k in (
            "name",
            "quantity",
            "kind",
            "location_id",
            "expiry_date",
            "purchase_date",
            "price_cents",
            "notes",
        )
    } == {
        "name": "Cordless drill",
        "quantity": 3,
        "kind": "belonging",
        "location_id": garage["shelf"],
        "expiry_date": "2031-02-03",
        "purchase_date": "2024-05-06",
        "price_cents": 12999,
        "notes": "18V, two batteries\nin the orange case",
    }
    # System-managed fields keep their defaults.
    assert row["needs_review"] == 0 and row["confidence"] is None and row["photo_id"] is None

    # The edit form shows every value back, and saving it unchanged changes nothing.
    edit = auth_client.get(f"/items/{row['id']}/edit")
    for value in (
        "Cordless drill",
        'value="3"',
        "Tools",
        "2031-02-03",
        "2024-05-06",
        "129.99",
        "in the orange case",
    ):
        assert value in edit.text
    assert '<option value="belonging" selected>' in edit.text
    saved = auth_client.post(
        f"/items/{row['id']}/edit",
        data={**FULL_FORM, "location_id": str(garage["shelf"])},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert _only_item(auth_settings)[0]["price_cents"] == 12999

    changed = {
        **FULL_FORM,
        "name": "Hammer",
        "quantity": "0",
        "kind": "consumable",
        "category": "",
        "expiry_date": "",
        "purchase_date": "",
        "price": "",
        "notes": "",
        "location_id": str(garage["garage"]),
    }
    assert auth_client.post(f"/items/{row['id']}/edit", data=changed).status_code == 200
    row, category = _only_item(auth_settings)
    assert category is None
    assert (row["name"], row["quantity"], row["kind"], row["location_id"]) == (
        "Hammer",
        0,
        "consumable",
        garage["garage"],
    )
    assert row["expiry_date"] is row["purchase_date"] is row["price_cents"] is row["notes"] is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "  "),
        ("quantity", "-1"),
        ("quantity", "1.5"),
        ("kind", "gadget"),
        ("expiry_date", "31/12/2030"),
        ("purchase_date", "yesterday"),
        ("price", "abc"),
        ("price", "1.234"),
        ("price", "-2"),
        ("location_id", "999999"),
    ],
)
def test_invalid_fields_rerender_with_400(auth_client, auth_settings, garage, field, value):
    data = {**FULL_FORM, "location_id": str(garage["shelf"]), field: value}
    response = auth_client.post("/items/new", data=data)
    assert response.status_code == 400
    assert "error" in response.text
    with open_db(auth_settings) as conn:
        assert conn.execute("SELECT count(*) FROM items").fetchone()[0] == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"ai_provider": "anthropic"},  # no API key: AI disabled
        {"monthly_ai_budget": 0, "ai_input_price_per_mtok": 3, "ai_output_price_per_mtok": 15},
    ],
)
def test_manual_add_works_without_ai(make_settings, overrides):
    settings = make_settings(household_password=TEST_PASSWORD, **overrides)
    with logged_in(settings) as client:
        assert client.get("/items/new").status_code == 200
        with open_db(settings) as conn:
            inbox = locations.inbox(conn)["id"]
        response = client.post(
            "/items/new",
            data={"name": "Milk", "quantity": "2", "kind": "consumable", "location_id": str(inbox)},
            follow_redirects=False,
        )
        assert response.status_code == 303


def _new_item(settings, location_id, quantity=1, name="Batteries"):
    with open_db(settings) as conn:
        return items.create(
            conn,
            items.ItemData(
                name=name, location_id=location_id, quantity=quantity, kind="consumable"
            ),
        )


def test_plus_minus_floor_at_zero_and_keep_item(auth_client, auth_settings, garage):
    item_id = _new_item(auth_settings, garage["shelf"], quantity=1)
    plus = auth_client.post(f"/items/{item_id}/quantity", data={"delta": "1"}, headers=HX)
    assert plus.status_code == 200
    assert (
        f'id="item-{item_id}"' in plus.text
        and '<span class="qty" aria-label="Quantity">2</span>' in plus.text
    )
    for expected in (1, 0, 0):
        response = auth_client.post(f"/items/{item_id}/quantity", data={"delta": "-1"}, headers=HX)
        assert response.status_code == 200
        assert f'aria-label="Quantity">{expected}</span>' in response.text
    assert "disabled" in response.text  # the -1 button is disabled at 0
    with open_db(auth_settings) as conn:
        row = items.get(conn, item_id)
    assert row is not None and row["quantity"] == 0


def test_quick_actions_require_htmx_and_valid_input(auth_client, auth_settings, garage):
    item_id = _new_item(auth_settings, garage["shelf"])
    assert auth_client.post(f"/items/{item_id}/quantity", data={"delta": "1"}).status_code == 403
    assert auth_client.post(f"/items/{item_id}/used-up").status_code == 403
    assert (
        auth_client.post(f"/items/{item_id}/quantity", data={"delta": "5"}, headers=HX).status_code
        == 400
    )
    assert (
        auth_client.post("/items/9999/quantity", data={"delta": "1"}, headers=HX).status_code == 404
    )
    other = TestClient(auth_client.app, headers={"Origin": "http://evil.example", **HX})
    other.cookies = auth_client.cookies
    assert other.post(f"/items/{item_id}/quantity", data={"delta": "1"}).status_code == 403


def test_used_up_and_undo(auth_client, auth_settings, garage):
    item_id = _new_item(auth_settings, garage["shelf"])
    used = auth_client.post(f"/items/{item_id}/used-up", headers=HX)
    assert used.status_code == 200
    assert "Undo" in used.text and f'hx-post="/items/{item_id}/restore"' in used.text
    assert "Batteries" not in auth_client.get(f"/l/{garage['code']}").text
    assert auth_client.get("/search?q=batt").text.count(f'id="item-{item_id}"') == 0
    assert auth_client.post(f"/items/{item_id}/used-up", headers=HX).status_code == 404

    undo = auth_client.post(f"/items/{item_id}/restore", headers=HX)
    assert undo.status_code == 200 and f'id="item-{item_id}"' in undo.text
    assert f'id="item-{item_id}"' in auth_client.get("/search?q=batt").text
    assert auth_client.post(f"/items/{item_id}/restore", headers=HX).status_code == 404


def test_move_item(auth_client, auth_settings, garage):
    item_id = _new_item(auth_settings, garage["shelf"])
    moved = auth_client.post(
        f"/items/{item_id}/move", data={"location_id": str(garage["garage"])}, headers=HX
    )
    assert moved.status_code == 200
    assert "Garage</a>" in moved.text and "Shelf 2" not in moved.text
    assert (
        auth_client.post(
            f"/items/{item_id}/move", data={"location_id": "99999"}, headers=HX
        ).status_code
        == 400
    )
    with open_db(auth_settings) as conn:
        assert items.get(conn, item_id)["location_id"] == garage["garage"]
        assert locations.recent(conn)[0]["id"] == garage["garage"]


def test_move_items_returns_previous_locations_for_undo(conn):
    a, b = make_chain(conn, ["A", "B"])
    first = items.create(conn, items.ItemData(name="one", location_id=a))
    second = items.create(conn, items.ItemData(name="two", location_id=b))
    previous = items.move_items(conn, [first, second], b)
    assert previous == {first: a}  # the second was already there
    for item_id, location_id in previous.items():
        items.move_item(conn, item_id, location_id)
    assert items.get(conn, first)["location_id"] == a


def test_price_parsing():
    assert items.parse_price("3.49") == 349
    assert items.parse_price("$1,5") == 150
    assert items.parse_price("7") == 700
    assert items.parse_price("") is None
    assert items.format_price(12999) == "129.99"
    assert items.format_price(5) == "0.05"

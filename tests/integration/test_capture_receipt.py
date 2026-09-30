"""Receipts go to the Inbox as product lines only, with price and purchase date (AC-17)."""

from datetime import UTC, datetime

import pytest

from app.ai.fake import FakeProvider
from app.ai.schemas import ReceiptMeta
from app.capture.apply import RECEIPT_DENYLIST
from app.inventory import locations
from tests.capture_helpers import (
    FIXTURES,
    capture_id,
    capture_items,
    capture_row,
    item,
    post_receipt,
    result,
    run,
    script_step,
)
from tests.conftest import TEST_PASSWORD, logged_in
from tests.inventory_helpers import open_db


@pytest.mark.parametrize("fixture", ["receipt.jpg", "receipt.pdf"])
def test_receipt_yields_only_product_lines_in_the_inbox(auth_client, auth_settings, fixture):
    response = post_receipt(auth_client, FIXTURES / fixture)
    assert response.status_code == 200
    cid = capture_id(response)
    provider = FakeProvider([script_step(1)])
    run(auth_settings, provider)
    assert provider.requests[0].mode == "receipt"

    with open_db(auth_settings) as conn:
        inbox = locations.inbox(conn)["id"]
        capture = capture_row(conn, cid)
        rows = capture_items(conn, cid)
        photo = conn.execute("SELECT id FROM photos WHERE capture_id = ?", (cid,)).fetchone()
    assert capture["status"] == "done"
    assert capture["merchant"] == "Corner Grocery"
    assert capture["location_id"] is None
    got = {row["name"]: (row["quantity"], row["price_cents"], row["purchase_date"]) for row in rows}
    assert got == {
        "Whole milk 1L": (2, 258, "2026-09-14"),  # receipt date
        "Sourdough bread": (1, 399, "2026-09-12"),  # the line's own date wins
        "Dish soap": (1, 329, "2026-09-14"),
    }
    assert all(row["location_id"] == inbox for row in rows)
    assert all(row["needs_review"] == 1 for row in rows)
    assert all(row["photo_id"] == photo["id"] for row in rows)

    status = auth_client.get(f"/captures/{cid}/status")
    assert status.status_code == 286
    assert (
        "3 items to" in status.text and "Inbox" in status.text and "Corner Grocery" in status.text
    )


def test_purchase_date_falls_back_to_the_capture_day_in_tz(make_settings):
    settings = make_settings(household_password=TEST_PASSWORD, tz="Pacific/Auckland")
    with logged_in(settings) as client:
        # 2026-09-20 13:00 UTC is already 2026-09-21 in Auckland.
        client.app.state.clock = lambda: datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
        cid = capture_id(post_receipt(client))
        run(
            settings,
            FakeProvider([result([item("Eggs", price_cents=450)], receipt=None)]),
        )
        with open_db(settings) as conn:
            rows = capture_items(conn, cid)
            capture = capture_row(conn, cid)
    assert [(r["name"], r["purchase_date"], r["price_cents"]) for r in rows] == [
        ("Eggs", "2026-09-21", 450)
    ]
    assert capture["merchant"] is None


def test_receipt_meta_without_date_also_falls_back(auth_client, auth_settings):
    auth_client.app.state.clock = lambda: datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
    cid = capture_id(post_receipt(auth_client))
    meta = ReceiptMeta(merchant="  Hardware Hut ", purchase_date=None)
    run(auth_settings, FakeProvider([result([item("Nails")], receipt=meta)]))
    with open_db(auth_settings) as conn:
        assert capture_items(conn, cid)[0]["purchase_date"] == "2026-09-03"
        assert capture_row(conn, cid)["merchant"] == "Hardware Hut"


@pytest.mark.parametrize(
    ("name", "dropped"),
    [
        ("Sales tax", True),
        ("TOTAL", True),
        ("Subtotal", True),
        ("Grocery bag", True),
        ("Bottle deposit", True),
        ("Change due", True),
        ("Tip", True),
        ("Bagels", False),
        ("Tiptree jam", False),
        ("Totally nuts granola", False),
        ("Oat milk", False),
    ],
)
def test_denylist_is_a_word_boundary_second_filter(name, dropped):
    assert bool(RECEIPT_DENYLIST.search(name)) is dropped

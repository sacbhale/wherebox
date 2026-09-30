"""A location deleted while its capture waits: nearest live ancestor or Inbox (AC-13)."""

import pytest

from app.ai.fake import FakeProvider
from app.inventory import locations
from tests.capture_helpers import (
    capture_id,
    capture_items,
    capture_row,
    item,
    post_photo,
    post_text,
    result,
    run,
    script_step,
)
from tests.inventory_helpers import make_chain, open_db


@pytest.fixture
def chain(auth_client, auth_settings) -> list[int]:
    with open_db(auth_settings) as conn:
        return make_chain(conn, ["Garage", "Shelf", "Box"])


def _placed(auth_settings, cid: int) -> set[tuple[int, int]]:
    with open_db(auth_settings) as conn:
        assert capture_row(conn, cid)["status"] == "done"
        return {(row["location_id"], row["needs_review"]) for row in capture_items(conn, cid)}


def test_items_go_to_the_nearest_live_ancestor_for_review(auth_client, auth_settings, chain):
    _, shelf, box = chain
    cid = capture_id(post_photo(auth_client, box))
    with open_db(auth_settings) as conn:
        locations.delete(conn, box)
    run(auth_settings, FakeProvider([script_step(0)]))
    assert _placed(auth_settings, cid) == {(shelf, 1)}  # even the confident items
    status = auth_client.get(f"/captures/{cid}/status")
    assert status.status_code == 286 and "Shelf" in status.text


def test_skips_deleted_ancestors(auth_client, auth_settings, chain):
    garage, shelf, box = chain
    cid = capture_id(post_photo(auth_client, box))
    with open_db(auth_settings) as conn:
        locations.delete(conn, box)
        locations.delete(conn, shelf)
    run(auth_settings, FakeProvider([script_step(0)]))
    assert _placed(auth_settings, cid) == {(garage, 1)}


def test_whole_branch_deleted_falls_back_to_inbox(auth_client, auth_settings, chain):
    garage, shelf, box = chain
    cid = capture_id(post_photo(auth_client, box))
    with open_db(auth_settings) as conn:
        for location_id in (box, shelf, garage):
            locations.delete(conn, location_id)
        inbox = locations.inbox(conn)["id"]
    run(auth_settings, FakeProvider([script_step(0)]))
    assert _placed(auth_settings, cid) == {(inbox, 1)}


def test_text_capture_uses_the_same_fallback(auth_client, auth_settings, chain):
    garage, shelf, box = chain
    cid = capture_id(post_text(auth_client, "a tape measure", box))
    with open_db(auth_settings) as conn:
        locations.delete(conn, box)
    run(
        auth_settings,
        FakeProvider([result([item("Tape measure"), item("Level", location_hint="Garage")])]),
    )
    assert _placed(auth_settings, cid) == {(shelf, 1), (garage, 0)}


def test_capture_into_a_deleted_location_is_refused(auth_client, auth_settings, chain):
    box = chain[-1]
    with open_db(auth_settings) as conn:
        locations.delete(conn, box)
    assert post_photo(auth_client, box).status_code == 400

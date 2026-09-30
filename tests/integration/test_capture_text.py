"""Text (and voice-typed) capture with location hints (AC-18)."""

import pytest

from app.ai.fake import FakeProvider
from app.inventory import locations
from tests.capture_helpers import (
    capture_id,
    capture_items,
    capture_row,
    item,
    post_text,
    result,
    run,
    script_step,
)
from tests.inventory_helpers import make_chain, open_db

SENTENCE = "Two cartons of milk, and the cordless drill is in the garage"


@pytest.fixture
def places(auth_client, auth_settings) -> dict[str, int]:
    with open_db(auth_settings) as conn:
        garage, shelf = make_chain(conn, ["Garage", "Shelf 1"])
        kitchen, _ = locations.create(conn, "Kitchen")
        shelf2, _ = locations.create(conn, "Shelf 2", garage)
        inbox = locations.inbox(conn)["id"]
    return {"garage": garage, "kitchen": kitchen, "shelf1": shelf, "shelf2": shelf2, "inbox": inbox}


def _names(auth_settings, cid: int) -> dict[str, tuple[int, int, int]]:
    with open_db(auth_settings) as conn:
        return {
            row["name"]: (row["quantity"], row["location_id"], row["needs_review"])
            for row in capture_items(conn, cid)
        }


def test_sentence_from_a_location_page(auth_client, auth_settings, places):
    response = post_text(auth_client, SENTENCE, places["kitchen"])
    assert response.status_code == 200
    cid = capture_id(response)
    provider = FakeProvider([script_step(2)])
    run(auth_settings, provider)

    request = provider.requests[0]
    assert request.mode == "text" and request.text == SENTENCE and request.images == []
    assert {"Garage", "Kitchen", "Shelf 1", "Inbox"} <= set(request.known_locations)
    assert _names(auth_settings, cid) == {
        "Milk": (2, places["kitchen"], 0),
        "Cordless drill": (1, places["garage"], 0),
    }
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, cid)
        assert capture["input_text"] == SENTENCE and capture["status"] == "done"
        assert all(row["photo_id"] is None for row in capture_items(conn, cid))


def test_sentence_from_home_goes_to_inbox_for_review(auth_client, auth_settings, places):
    cid = capture_id(post_text(auth_client, SENTENCE))
    run(auth_settings, FakeProvider([script_step(2)]))
    assert _names(auth_settings, cid) == {
        "Milk": (2, places["inbox"], 1),
        "Cordless drill": (1, places["garage"], 0),
    }


@pytest.mark.parametrize(
    ("hint", "where", "review"),
    [
        ("garage", "garage", 0),  # case-insensitive exact, although "Garage" is also a prefix
        ("  KITCHEN ", "kitchen", 0),
        ("kit", "kitchen", 0),  # unique prefix
        ("Shelf 2", "shelf2", 0),
        ("shelf", "kitchen", 1),  # ambiguous prefix: capture location, needs review
        ("Attic", "kitchen", 1),  # no match
        (None, "kitchen", 0),  # no hint: the capture's location
    ],
)
def test_location_hint_matching(auth_client, auth_settings, places, hint, where, review):
    cid = capture_id(post_text(auth_client, "a hammer", places["kitchen"]))
    run(auth_settings, FakeProvider([result([item("Hammer", location_hint=hint)])]))
    assert _names(auth_settings, cid) == {"Hammer": (1, places[where], review)}


def test_deleted_locations_are_not_matched(auth_client, auth_settings, places):
    with open_db(auth_settings) as conn:
        attic, _ = locations.create(conn, "Attic")
        locations.delete(conn, attic)
    cid = capture_id(post_text(auth_client, "a lamp in the attic"))
    run(auth_settings, FakeProvider([result([item("Lamp", location_hint="Attic")])]))
    assert _names(auth_settings, cid) == {"Lamp": (1, places["inbox"], 1)}


def test_low_confidence_text_items_need_review(auth_client, auth_settings, places):
    cid = capture_id(post_text(auth_client, "maybe a wrench", places["kitchen"]))
    run(auth_settings, FakeProvider([result([item("Wrench", 0.4, location_hint="Garage")])]))
    assert _names(auth_settings, cid) == {"Wrench": (1, places["garage"], 1)}


def test_text_validation(auth_client, places):
    assert post_text(auth_client, "   ", places["kitchen"]).status_code == 400
    assert post_text(auth_client, "x" * 2001).status_code == 400
    assert post_text(auth_client, "milk", 999_999).status_code == 400

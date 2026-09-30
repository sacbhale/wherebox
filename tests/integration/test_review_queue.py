"""The review queue (AC-7): every action is one hx-post whose answer removes the row; Edit is
one link to the form. Also the capture photo route used for thumbnails."""

import re
import shutil

import pytest

from app.ai.fake import FakeProvider
from app.capture import reconcile
from app.db.connection import write_tx
from app.inventory import items, locations, search
from tests.capture_helpers import FIXTURES, capture_id, capture_row, post_text, result, run
from tests.capture_helpers import item as extracted
from tests.inventory_helpers import HX, open_db


@pytest.fixture
def queue(auth_client, auth_settings) -> dict[str, int]:
    """One of each row: a failed capture, a flagged item (with a photo), a missing proposal
    and a quantity proposal."""
    with open_db(auth_settings) as conn:
        shelf, _ = locations.create(conn, "Shelf")
    cid = capture_id(post_text(auth_client, "a mystery box", shelf))
    run(auth_settings, FakeProvider([], repeat_last=False))  # empty script: permanent failure
    target = auth_settings.data_dir / "photos" / "2026" / "09" / "abc.jpg"
    target.parent.mkdir(parents=True)
    shutil.copy(FIXTURES / "shelf.jpg", target)
    with open_db(auth_settings) as conn:
        assert capture_row(conn, cid)["status"] == "failed"
        with write_tx(conn):
            photo_id = conn.execute(
                "INSERT INTO photos (capture_id, rel_path, sha256, created_at)"
                " VALUES (NULL, 'photos/2026/09/abc.jpg', 'x', '2026-09-01T00:00:00Z')"
            ).lastrowid
            flagged = items.insert(
                conn,
                items.ItemData(name="Blurry thing", location_id=shelf),
                items.SystemFields(photo_id=photo_id, confidence=0.4, needs_review=True),
            )
            gone = items.insert(conn, items.ItemData(name="Old glue", location_id=shelf))
            counted = items.insert(
                conn, items.ItemData(name="Screws", location_id=shelf, quantity=10)
            )
            search.reindex_items(conn, [flagged, gone, counted])
            reconcile.propose(conn, "missing", gone, None)
            reconcile.propose(conn, "quantity", counted, None, 7)
        missing_id, quantity_id = (
            row[0] for row in conn.execute("SELECT id FROM review_proposals ORDER BY id")
        )
    return {
        "capture": cid,
        "flagged": flagged,
        "gone": gone,
        "counted": counted,
        "missing": missing_id,
        "quantity": quantity_id,
        "photo": photo_id,
    }


def one_hx_post(page: str, row_id: str, button_class: str) -> str:
    """The row's single hx-post URL for ``button_class`` (asserting there is exactly one)."""
    row = re.search(rf'<li class="review-row" id="{row_id}".*?</li>', page, re.DOTALL)
    assert row, f"no row {row_id}"
    buttons = re.findall(rf'<button[^>]*class="[^"]*\b{button_class}\b[^"]*"[^>]*>', row.group(0))
    assert len(buttons) == 1, buttons
    button = buttons[0]
    assert 'hx-target="closest .review-row"' in button and 'hx-swap="outerHTML"' in button
    posts = re.findall(r'hx-post="([^"]+)"', button)
    assert len(posts) == 1
    return posts[0]


def fts_ids(conn) -> set[int]:
    return {row[0] for row in conn.execute("SELECT rowid FROM items_fts")}


def test_page_lists_all_four_sections(auth_client, queue):
    page = auth_client.get("/review")
    assert page.status_code == 200
    for section in ("review-failed", "review-items", "review-missing", "review-quantity"):
        assert f'id="{section}"' in page.text
    assert f'id="review-capture-{queue["capture"]}"' in page.text
    assert f'id="review-item-{queue["flagged"]}"' in page.text
    assert f'id="review-proposal-{queue["missing"]}"' in page.text
    assert f'id="review-proposal-{queue["quantity"]}"' in page.text
    assert "Accept 7" in page.text and "recorded 10, counted 7" in page.text
    assert f'src="/photos/{queue["photo"]}"' in page.text
    assert 'href="/review"' in page.text  # nav link


def test_empty_queue(auth_client):
    page = auth_client.get("/review")
    assert page.status_code == 200 and 'id="review-empty"' in page.text


def test_keep_clears_needs_review(auth_client, auth_settings, queue):
    url = one_hx_post(
        auth_client.get("/review").text, f"review-item-{queue['flagged']}", "review-keep"
    )
    response = auth_client.post(url, headers=HX)
    assert response.status_code == 200 and response.text == ""
    with open_db(auth_settings) as conn:
        row = items.get(conn, queue["flagged"])
    assert row["needs_review"] == 0 and row["deleted_at"] is None
    assert f'id="review-item-{queue["flagged"]}"' not in auth_client.get("/review").text
    assert auth_client.post(url, headers=HX).status_code == 404  # nothing left to keep


def test_delete_soft_deletes_and_drops_the_search_row(auth_client, auth_settings, queue):
    page = auth_client.get("/review").text
    url = one_hx_post(page, f"review-item-{queue['flagged']}", "review-delete")
    response = auth_client.post(url, headers=HX)
    assert response.status_code == 200 and response.text == ""
    with open_db(auth_settings) as conn:
        row = items.get(conn, queue["flagged"], include_deleted=True)
        indexed = fts_ids(conn)
    assert row["deleted_at"] is not None
    assert queue["flagged"] not in indexed


def test_edit_is_one_link_and_saving_clears_needs_review(auth_client, auth_settings, queue):
    page = auth_client.get("/review").text
    row = re.search(rf'id="review-item-{queue["flagged"]}".*?</li>', page, re.DOTALL).group(0)
    edit_links = re.findall(r'<a class="[^"]*review-edit[^"]*" href="([^"]+)"', row)
    assert edit_links == [f"/items/{queue['flagged']}/edit?from=review"]

    form = auth_client.get(edit_links[0])
    assert form.status_code == 200
    assert '<input type="hidden" name="from" value="review">' in form.text
    with open_db(auth_settings) as conn:
        shelf = items.get(conn, queue["flagged"])["location_id"]
    saved = auth_client.post(
        f"/items/{queue['flagged']}/edit",
        data={
            "from": "review",
            "name": "Sharp thing",
            "quantity": "3",
            "kind": "belonging",
            "location_id": str(shelf),
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/review"
    with open_db(auth_settings) as conn:
        row = items.get(conn, queue["flagged"])
    assert (row["name"], row["quantity"], row["needs_review"]) == ("Sharp thing", 3, 0)
    assert row["photo_id"] == queue["photo"] and row["confidence"] == pytest.approx(0.4)


def test_plain_edit_keeps_needs_review(auth_client, auth_settings, queue):
    form = auth_client.get(f"/items/{queue['flagged']}/edit")
    assert 'name="from"' not in form.text
    with open_db(auth_settings) as conn:
        shelf = items.get(conn, queue["flagged"])["location_id"]
    saved = auth_client.post(
        f"/items/{queue['flagged']}/edit",
        data={"name": "Renamed", "quantity": "1", "kind": "belonging", "location_id": str(shelf)},
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"].startswith("/l/")
    with open_db(auth_settings) as conn:
        assert items.get(conn, queue["flagged"])["needs_review"] == 1


def proposal(conn, proposal_id: int):
    return conn.execute("SELECT * FROM review_proposals WHERE id = ?", (proposal_id,)).fetchone()


def test_confirm_removal_uses_up_the_item(auth_client, auth_settings, queue):
    page = auth_client.get("/review").text
    url = one_hx_post(page, f"review-proposal-{queue['missing']}", "review-confirm-missing")
    response = auth_client.post(url, headers=HX)
    assert response.status_code == 200 and response.text == ""
    with open_db(auth_settings) as conn:
        row = items.get(conn, queue["gone"], include_deleted=True)
        prop = proposal(conn, queue["missing"])
        indexed = fts_ids(conn)
    assert row["deleted_at"] is not None and queue["gone"] not in indexed
    assert prop["status"] == "accepted" and prop["resolved_at"] is not None
    assert auth_client.post(url, headers=HX).status_code == 404


def test_still_here_dismisses_and_marks_seen(auth_client, auth_settings, queue):
    page = auth_client.get("/review").text
    url = one_hx_post(page, f"review-proposal-{queue['missing']}", "review-still-here")
    response = auth_client.post(url, headers=HX)
    assert response.status_code == 200 and response.text == ""
    with open_db(auth_settings) as conn:
        row = items.get(conn, queue["gone"])
        prop = proposal(conn, queue["missing"])
    assert row["deleted_at"] is None and row["last_seen_at"] is not None
    assert prop["status"] == "dismissed" and prop["resolved_at"] is not None


def test_accept_quantity_sets_the_count(auth_client, auth_settings, queue):
    page = auth_client.get("/review").text
    url = one_hx_post(page, f"review-proposal-{queue['quantity']}", "review-accept")
    response = auth_client.post(url, headers=HX)
    assert response.status_code == 200 and response.text == ""
    with open_db(auth_settings) as conn:
        assert items.get(conn, queue["counted"])["quantity"] == 7
        assert proposal(conn, queue["quantity"])["status"] == "accepted"


def test_dismiss_quantity_keeps_the_count(auth_client, auth_settings, queue):
    page = auth_client.get("/review").text
    url = one_hx_post(page, f"review-proposal-{queue['quantity']}", "review-dismiss")
    response = auth_client.post(url, headers=HX)
    assert response.status_code == 200 and response.text == ""
    with open_db(auth_settings) as conn:
        assert items.get(conn, queue["counted"])["quantity"] == 10
        assert proposal(conn, queue["quantity"])["status"] == "dismissed"


def test_proposal_actions_check_the_proposal_type(auth_client, queue):
    assert (
        auth_client.post(
            f"/review/proposals/{queue['quantity']}/still-here", headers=HX
        ).status_code
        == 404
    )
    assert (
        auth_client.post(f"/review/proposals/{queue['missing']}/accept", headers=HX).status_code
        == 404
    )


def test_retry_requeues_the_failed_capture(auth_client, auth_settings, queue):
    page = auth_client.get("/review").text
    url = one_hx_post(page, f"review-capture-{queue['capture']}", "review-retry")
    response = auth_client.post(url, headers=HX)
    assert response.status_code == 200 and response.text == ""
    with open_db(auth_settings) as conn:
        capture = capture_row(conn, queue["capture"])
    assert capture["status"] == "queued" and capture["attempts"] == 0
    assert capture["error"] is None
    assert run(auth_settings, FakeProvider([result([extracted("Box")])])) is True
    with open_db(auth_settings) as conn:
        assert capture_row(conn, queue["capture"])["status"] == "done"
    assert auth_client.post(url, headers=HX).status_code == 404  # no longer failed


def test_review_actions_need_htmx(auth_client, queue):
    response = auth_client.post(f"/review/items/{queue['flagged']}/keep")
    assert response.status_code in (400, 403)


def test_photo_route_serves_the_stored_file(auth_client, queue):
    response = auth_client.get(f"/photos/{queue['photo']}")
    assert response.status_code == 200
    assert response.content == (FIXTURES / "shelf.jpg").read_bytes()
    assert response.headers["content-type"] == "image/jpeg"
    assert auth_client.get("/photos/99999").status_code == 404


@pytest.mark.parametrize("rel_path", ["../app.db", "photos/../../etc/passwd", "/etc/passwd"])
def test_photo_route_cannot_escape_the_photos_directory(auth_client, auth_settings, rel_path):
    with open_db(auth_settings) as conn, write_tx(conn):
        photo_id = conn.execute(
            "INSERT INTO photos (capture_id, rel_path, sha256, created_at)"
            " VALUES (NULL, ?, 'x', '2026-09-01T00:00:00Z')",
            (rel_path,),
        ).lastrowid
    assert auth_client.get(f"/photos/{photo_id}").status_code == 404

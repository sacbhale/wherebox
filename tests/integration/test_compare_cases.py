"""Comparison cases (spec B2, R5): "Use for model comparison" on finished photo, receipt and
text captures, the pre-filled form, saving with edits, validation, reconcile/unfinished
captures rejected, input-missing flag, edit, and soft delete (old reports keep the case)."""

import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ai import openrouter_catalog
from app.capture import service
from app.compare import cases, planning, report, runs
from app.compare.scoring import Expected
from app.config import Settings
from app.inventory import items
from tests.compare_helpers import CATALOG, T0, done_capture, extract, make_case
from tests.conftest import TEST_PASSWORD, SettingsFactory, logged_in
from tests.inventory_helpers import open_db


@pytest.fixture(autouse=True)
def _fresh_catalog() -> Iterator[None]:
    openrouter_catalog.clear_cache()
    yield
    openrouter_catalog.clear_cache()


@pytest.fixture
def auth_settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(
        household_password=TEST_PASSWORD, openrouter_catalog_file=CATALOG, ai_model="test/model-a"
    )


def _db(settings: Settings):
    return open_db(settings)


def _form_rows(html: str) -> list[tuple[str, str]]:
    """(name, quantity) of every filled row of the case form."""
    form = html.split('id="case-form"', 1)[1].split("<template", 1)[0]
    names = re.findall(r'name="name" value="([^"]*)"', form)
    quantities = re.findall(r'name="quantity" value="([^"]*)"', form)
    return [(n, q) for n, q in zip(names, quantities, strict=True) if n]


def _case_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM benchmark_cases ORDER BY id").fetchall()


def test_captures_list_offers_eligible_captures(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    with _db(auth_settings) as conn:
        text = done_capture(conn, auth_settings, "text", extract("Milk"), text="milk")
        recon = done_capture(conn, auth_settings, "reconcile", extract())
        queued = service.create_capture(conn, capture_type="text", now=T0, input_text="x")
    html = auth_client.get("/captures").text
    assert f'href="/compare/cases/new?capture_id={text}"' in html
    assert f"capture_id={recon}" not in html
    assert f"capture_id={queued}" not in html


def test_text_case_prefilled_edited_and_saved(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    with _db(auth_settings) as conn:
        cid = done_capture(
            conn,
            auth_settings,
            "text",
            extract(("Milk", 2), "Cordless drill"),
            text="two milks and a drill",
            model="test/model-b",
        )
    page = auth_client.get(f"/compare/cases/new?capture_id={cid}")
    assert page.status_code == 200
    assert _form_rows(page.text) == [("Milk", "2"), ("Cordless drill", "1")]
    assert "two milks and a drill" in page.text  # the original input, to check against
    assert "captured with <code>test/model-b</code>" in page.text
    assert 'name="price"' not in page.text
    assert 'id="case-prefill-hint"' not in page.text

    saved = auth_client.post(
        "/compare/cases",
        data={
            "capture_id": str(cid),
            "name": ["Milk", "", "Cordless drill", "Duct tape"],
            "quantity": ["3", "1", "1", ""],
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303 and saved.headers["location"] == "/compare"
    with _db(auth_settings) as conn:
        case = cases.for_capture(conn, auth_settings, cid)
        assert case is not None and case.usable
        assert case.expected == [
            Expected("Milk", 3),
            Expected("Cordless drill", 1),
            Expected("Duct tape", 1),
        ]
        assert case.capture_model == "test/model-b"
    # Opening the capture again edits its case instead of making a second one.
    again = auth_client.get(f"/compare/cases/new?capture_id={cid}", follow_redirects=False)
    assert again.status_code == 303
    assert again.headers["location"] == f"/compare/cases/{case.id}/edit"


def test_photo_case(auth_client: TestClient, auth_settings: Settings) -> None:
    with _db(auth_settings) as conn:
        cid = done_capture(conn, auth_settings, "photo", extract("AA batteries", "Duct tape"))
    page = auth_client.get(f"/compare/cases/new?capture_id={cid}")
    assert _form_rows(page.text) == [("AA batteries", "1"), ("Duct tape", "1")]
    assert "Open the original photo" in page.text
    response = auth_client.post(
        "/compare/cases",
        data={"capture_id": str(cid), "name": ["AA batteries", "Hammer"], "quantity": ["8", "1"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _db(auth_settings) as conn:
        case = cases.for_capture(conn, auth_settings, cid)
        assert case is not None and case.type == "photo" and not case.input_missing
        assert [e.name for e in case.expected] == ["AA batteries", "Hammer"]


def test_receipt_case_with_prices(auth_client: TestClient, auth_settings: Settings) -> None:
    with _db(auth_settings) as conn:
        cid = done_capture(
            conn, auth_settings, "receipt", extract("Whole milk", "Bread", price=258)
        )
    page = auth_client.get(f"/compare/cases/new?capture_id={cid}")
    assert 'name="price" value="2.58"' in page.text
    response = auth_client.post(
        "/compare/cases",
        data={
            "capture_id": str(cid),
            "name": ["Whole milk", "Bread"],
            "quantity": ["2", "1"],
            "price": ["2.58", "$3.99"],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    with _db(auth_settings) as conn:
        case = cases.for_capture(conn, auth_settings, cid)
        assert case is not None
        assert case.expected == [Expected("Whole milk", 2, 258), Expected("Bread", 1, 399)]


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"name": ["Milk"], "quantity": ["0"]}, "quantity must be a whole number"),
        ({"name": ["Milk"], "quantity": ["two"]}, "quantity must be a whole number"),
        ({"name": [""], "quantity": ["1"]}, "Add at least one item."),
        ({"name": ["x" * 201], "quantity": ["1"]}, "under 200 characters"),
    ],
)
def test_invalid_rows_are_shown_again(
    auth_client: TestClient, auth_settings: Settings, data: dict, message: str
) -> None:
    with _db(auth_settings) as conn:
        cid = done_capture(conn, auth_settings, "text", extract("Milk"), text="milk")
    response = auth_client.post("/compare/cases", data={"capture_id": str(cid)} | data)
    assert response.status_code == 400
    assert message in response.text
    with _db(auth_settings) as conn:
        assert _case_rows(conn) == []


def test_bad_receipt_price(auth_client: TestClient, auth_settings: Settings) -> None:
    with _db(auth_settings) as conn:
        cid = done_capture(conn, auth_settings, "receipt", extract("Milk", price=100))
    response = auth_client.post(
        "/compare/cases",
        data={"capture_id": str(cid), "name": ["Milk"], "quantity": ["1"], "price": ["abc"]},
    )
    assert response.status_code == 400
    assert "Price must be a number" in response.text
    assert 'value="abc"' in response.text  # redisplayed as typed


def test_reconcile_and_unfinished_captures_are_rejected(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    with _db(auth_settings) as conn:
        recon = done_capture(conn, auth_settings, "reconcile", extract())
        queued = service.create_capture(conn, capture_type="text", now=T0, input_text="x")
    for cid in (recon, queued):
        page = auth_client.get(f"/compare/cases/new?capture_id={cid}")
        assert page.status_code == 409
        posted = auth_client.post(
            "/compare/cases", data={"capture_id": str(cid), "name": ["Milk"], "quantity": ["1"]}
        )
        assert posted.status_code == 409
    assert (
        "Only photo, receipt and text captures"
        in auth_client.get(f"/compare/cases/new?capture_id={recon}").text
    )
    assert auth_client.get("/compare/cases/new?capture_id=999").status_code == 404
    with _db(auth_settings) as conn:
        assert _case_rows(conn) == []
        with pytest.raises(cases.CaseError):
            cases.save(conn, recon, [Expected("Milk")])


def test_prefill_hint_when_items_changed_since(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    with _db(auth_settings) as conn:
        cid = done_capture(conn, auth_settings, "text", extract("Milk", "Eggs"), text="milk eggs")
        milk = conn.execute("SELECT id FROM items WHERE capture_id = ?", (cid,)).fetchone()[0]
        items.use_up(conn, milk)
    page = auth_client.get(f"/compare/cases/new?capture_id={cid}")
    assert 'id="case-prefill-hint"' in page.text
    assert _form_rows(page.text) == [("Milk", "1"), ("Eggs", "1")]  # used up ones included


def test_missing_input_is_flagged_and_skipped(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    with _db(auth_settings) as conn:
        photo_case = make_case(conn, auth_settings, ["Hammer"], capture_type="photo")
        text_case = make_case(conn, auth_settings, ["Milk"])
        rel = conn.execute(
            "SELECT p.rel_path FROM photos p JOIN benchmark_cases b"
            " ON b.capture_id = p.capture_id WHERE b.id = ?",
            (photo_case,),
        ).fetchone()[0]
    Path(auth_settings.data_dir / rel).unlink()
    html = auth_client.get("/compare").text
    assert "Input missing: this case is skipped." in html
    assert f'name="cases" value="{text_case}"' in html
    assert f'name="cases" value="{photo_case}"' not in html
    with _db(auth_settings) as conn:
        chosen = planning.select_cases(conn, auth_settings, None)
        assert [c.id for c in chosen] == [text_case]
        with pytest.raises(planning.PlanError):
            planning.select_cases(conn, auth_settings, [photo_case])


def test_edit_and_soft_delete_keep_old_reports(
    auth_client: TestClient, auth_settings: Settings
) -> None:
    with _db(auth_settings) as conn:
        case_id = make_case(conn, auth_settings, ["Milk", "Eggs"])
        other = make_case(conn, auth_settings, ["Bread"])
        run_id = runs.start(conn, ["test/model-a", "test/model-b"], [case_id, other], 0, T0)

    edit = auth_client.get(f"/compare/cases/{case_id}/edit")
    assert edit.status_code == 200
    assert _form_rows(edit.text) == [("Milk", "1"), ("Eggs", "1")]
    updated = auth_client.post(
        f"/compare/cases/{case_id}",
        data={"name": ["Milk", "Eggs", "Butter"], "quantity": ["1", "12", "1"]},
        follow_redirects=False,
    )
    assert updated.status_code == 303

    deleted = auth_client.post(f"/compare/cases/{case_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert auth_client.post(f"/compare/cases/{case_id}/delete").status_code == 404
    assert auth_client.get(f"/compare/cases/{case_id}/edit").status_code == 404
    with _db(auth_settings) as conn:
        (row,) = [r for r in _case_rows(conn) if r["id"] == case_id]
        assert row["deleted_at"] is not None  # soft delete: the row stays
        assert [c.id for c in cases.live(conn, auth_settings)] == [other]
        assert cases.count_live(conn) == 1
        # The old run's report still shows the deleted case.
        data = report.build(conn, auth_settings, run_id)
        assert data is not None and [v.case_id for v in data.cases] == [case_id, other]
    html = auth_client.get(f"/compare/runs/{run_id}").text
    assert "case deleted since" in html
    assert "1 comparison case" in auth_client.get("/compare").text

    # Making a case from the same capture again restores it with the new list.
    with _db(auth_settings) as conn:
        capture_id = conn.execute(
            "SELECT capture_id FROM benchmark_cases WHERE id = ?", (case_id,)
        ).fetchone()[0]
    page = auth_client.get(f"/compare/cases/new?capture_id={capture_id}")
    assert page.status_code == 200
    auth_client.post(
        "/compare/cases",
        data={"capture_id": str(capture_id), "name": ["Oat milk"], "quantity": ["1"]},
    )
    with _db(auth_settings) as conn:
        restored = cases.get(conn, auth_settings, case_id)
        assert restored is not None and restored.deleted_at is None
        assert restored.expected == [Expected("Oat milk", 1)]


def test_case_writes_need_same_origin(auth_settings: Settings) -> None:
    with logged_in(auth_settings) as client:
        with _db(auth_settings) as conn:
            cid = done_capture(conn, auth_settings, "text", extract("Milk"), text="milk")
        cross = client.post(
            "/compare/cases",
            data={"capture_id": str(cid), "name": ["Milk"], "quantity": ["1"]},
            headers={"Origin": "http://evil.example"},
        )
        assert cross.status_code == 403
    with _db(auth_settings) as conn:
        assert _case_rows(conn) == []


def test_comparison_code_never_deletes() -> None:
    """Spec R5: no DELETE on cases, runs, results or ai_calls (soft delete instead)."""
    compare_dir = Path(__file__).resolve().parents[2] / "app" / "compare"
    sources = [path.read_text("utf-8") for path in sorted(compare_dir.glob("*.py"))]
    assert len(sources) >= 6
    assert not any(re.search(r"\bDELETE\s+FROM\b", source, re.IGNORECASE) for source in sources)

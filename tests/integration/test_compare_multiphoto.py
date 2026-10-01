"""Comparisons with multi-photo capture groups (spec-multiphoto Part 6, R9, R10): cases keyed on
the root, the group's photos captured at case creation, older cases falling back to their
capture's photos, every case photo sent in order with the plain shelf prompt, boxes validated
into ``items_json``, image counts in the ledger, image-aware estimates and report boxes."""

import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.ai import openrouter_catalog
from app.ai.fake import FakeProvider
from app.ai.provider import ProviderTimeoutError
from app.ai.schemas import ExtractRequest, ExtractResult, Usage
from app.capture import budget, groups, service, worker
from app.compare import cases, execution, planning, report, runs
from app.compare.scoring import Expected
from app.config import Settings
from app.db.connection import write_tx
from app.db.migrate import migrate
from tests.capture_helpers import FIXTURES, colour, image_colour, item, make_jpegs, result
from tests.compare_helpers import CATALOG, T0, done_capture, extract
from tests.conftest import TEST_PASSWORD, SettingsFactory, logged_in
from tests.inventory_helpers import open_db


@pytest.fixture(autouse=True)
def _fresh_catalog():
    openrouter_catalog.clear_cache()
    yield
    openrouter_catalog.clear_cache()


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(openrouter_catalog_file=CATALOG, ai_model="test/model-a")


def _save(settings: Settings, paths: list[Path]) -> list[service.SavedUpload]:
    saved = []
    for path in paths:
        with path.open("rb") as fh:
            saved.append(service.save_upload(settings, fh, path.name, T0))
    return saved


def group_with_extension(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> tuple[int, int]:
    """A done 2-photo root (Tape, Glue) plus a done 1-photo extension (Saw)."""
    inbox = conn.execute("SELECT id FROM locations WHERE is_system = 1").fetchone()[0]
    root = service.create_capture(
        conn,
        capture_type="photo",
        now=T0,
        location_id=inbox,
        uploads=_save(settings, make_jpegs(tmp_path / "root", 2)),
    )
    assert worker.run_once(conn, settings, FakeProvider([result([item("Tape"), item("Glue")])]))
    ext = service.create_capture(
        conn,
        capture_type="photo",
        now=T0,
        uploads=_save(settings, make_jpegs(tmp_path / "ext", 1, (300, 200))[0:1]),
        extends=root,
    )
    fake = FakeProvider([result([item("Saw")])])
    assert worker.run_once(conn, settings, fake)
    return root, ext


def test_a_case_from_an_extension_is_keyed_on_the_root_with_the_group_photos(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    root, ext = group_with_extension(conn, settings, tmp_path)
    filled = cases.prefill(conn, ext)
    assert [e.name for e in filled.items] == ["Tape", "Glue", "Saw"]
    case_id = cases.save(conn, ext, filled.items)
    case = cases.get(conn, settings, case_id)
    assert case is not None and case.capture_id == root
    assert case.photo_ids == groups.done_photo_ids(conn, root)
    assert len(case.photo_ids) == 3 and case.image_count == 3
    assert case.photo_id == case.photo_ids[0] and not case.input_missing
    assert cases.for_capture(conn, settings, root).id == case_id
    stored = conn.execute("SELECT photo_ids_json FROM benchmark_cases").fetchone()[0]
    assert json.loads(stored) == case.photo_ids
    # Saving again from the root updates the same case.
    assert cases.save(conn, root, [Expected("Tape")]) == case_id
    assert cases.case_capture_id(conn, ext) == root


def test_case_routes_send_an_extension_to_its_root(make_settings, tmp_path) -> None:
    settings = make_settings(
        household_password=TEST_PASSWORD, openrouter_catalog_file=CATALOG, ai_model="test/model-a"
    )
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            root, ext = group_with_extension(conn, settings, tmp_path)
        form = client.get("/compare/cases/new", params={"capture_id": ext})
        assert form.status_code == 200
        assert f'name="capture_id" value="{root}"' in form.text
        assert 'value="Saw"' in form.text and 'value="Tape"' in form.text
        saved = client.post(
            "/compare/cases",
            data={"capture_id": str(ext), "name": ["Tape", "Saw"], "quantity": ["1", "1"]},
            follow_redirects=False,
        )
        assert saved.status_code == 303
        again = client.get("/compare/cases/new", params={"capture_id": ext}, follow_redirects=False)
        assert again.status_code == 303 and again.headers["location"].endswith("/edit")
        with open_db(settings) as conn:
            rows = conn.execute("SELECT capture_id FROM benchmark_cases").fetchall()
        assert [r[0] for r in rows] == [root]


def test_old_cases_fall_back_to_their_capture_photos_and_missing_photos_flag_the_case(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    root, _ext = group_with_extension(conn, settings, tmp_path)
    case_id = cases.save(conn, root, [Expected("Tape")])
    with write_tx(conn):
        conn.execute("UPDATE benchmark_cases SET photo_ids_json = NULL")  # a pre-0005 case
    case = cases.get(conn, settings, case_id)
    root_photos = [
        r[0]
        for r in conn.execute(
            "SELECT id FROM photos WHERE capture_id = ? ORDER BY position", (root,)
        )
    ]
    assert case.photo_ids == root_photos and not case.input_missing
    (settings.data_dir / case.rel_paths[1]).unlink()
    assert cases.get(conn, settings, case_id).input_missing
    assert not cases.get(conn, settings, case_id).usable


def test_comparison_calls_send_every_case_photo_and_store_validated_boxes(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    root, _ext = group_with_extension(conn, settings, tmp_path)
    case_id = cases.save(conn, root, [Expected("Tape"), Expected("Saw")])
    photos = cases.get(conn, settings, case_id).photo_ids
    answer = result(
        [
            item(
                "Tape",
                boxes=[
                    {"image": 0, "box_2d": [0, 0, 500, 500]},
                    {"image": 2, "box_2d": [100, 200, 300, 400]},
                    {"image": 3, "box_2d": [0, 0, 10, 10]},  # only 3 images: dropped
                    {"image": 1, "box_2d": "x"},
                ],
            ),
            item("Saw"),
        ]
    )
    fake = FakeProvider([answer], model="test/model-a")
    run_id = runs.start(conn, ["test/model-a"], [case_id], 0, datetime.now(UTC))
    assert execution.run_once(conn, settings, fake.with_model)
    request = fake.requests[0]
    assert request.mode == "shelf" and request.existing_items == []
    assert len(request.images) == 3
    assert [image_colour(i) for i in request.images[:2]] == [
        pytest.approx(colour(0), abs=12),
        pytest.approx(colour(1), abs=12),
    ]
    assert request.timeout_s == settings.ai_timeout_seconds + 20
    (row,) = runs.results(conn, run_id)
    stored = json.loads(row["items_json"])
    assert stored[0]["boxes"] == [
        {"image": 0, "photo_id": photos[0], "x0": 0.0, "y0": 0.0, "x1": 0.5, "y1": 0.5},
        {"image": 2, "photo_id": photos[2], "x0": 0.2, "y0": 0.1, "x1": 0.4, "y1": 0.3},
    ]
    assert stored[1]["boxes"] == []
    assert json.loads(row["scores_json"])["f1"] == 1.0  # scoring unchanged
    (call,) = conn.execute(
        "SELECT image_count FROM ai_calls WHERE comparison_result_id = ?", (row["id"],)
    ).fetchall()
    assert call[0] == 3

    view = report.build(conn, settings, run_id)
    case_view = view.cases[0]
    assert [p.id for p in case_view.photos] == photos
    lines = case_view.models[0].lines
    assert [(line.number, line.mark) for line in lines] == [(1, "matched"), (2, "matched")]
    assert lines[0].boxes[1]["photo_id"] == photos[2]


def test_estimates_follow_the_case_photo_counts(
    conn: sqlite3.Connection, settings: Settings, tmp_path: Path
) -> None:
    catalog = openrouter_catalog.load_catalog(settings)
    model = catalog.get("test/model-a")  # $0.50 / $2.00 per 1M
    one = planning.estimate(conn, [model], [1])
    three = planning.estimate(conn, [model], [3])
    text = planning.estimate(conn, [model], [0])
    assert one.tokens == (1_500, 400, False)
    assert three.tokens == (3_900, 1_000, False)
    assert one.total_micros == round(1_500 * 0.5 + 400 * 2)
    assert three.total_micros == round(3_900 * 0.5 + 1_000 * 2)
    assert text.total_micros == one.total_micros  # text: the per-call default
    both = planning.estimate(conn, [model], [1, 3])
    assert both.total_micros == one.total_micros + three.total_micros
    assert both.tokens[:2] == (2_700, 700)
    root, _ext = group_with_extension(conn, settings, tmp_path)
    case_id = cases.save(conn, root, [Expected("Tape")])
    chosen = planning.select_cases(conn, settings, [case_id])
    assert [c.image_count for c in chosen] == [3]


class _TimingOut:
    name = "openrouter"
    model = "test/model-a"
    max_tokens = 4096

    def extract(self, req: ExtractRequest) -> ExtractResult:
        raise ProviderTimeoutError(maybe_billed=True)


def test_maybe_billed_photo_calls_are_charged_for_their_images_and_output_cap(
    make_settings: SettingsFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(openrouter_catalog, "fetch_models", lambda: json.loads(CATALOG.read_text()))
    settings = make_settings(
        ai_provider="openrouter", openrouter_api_key="k", openrouter_catalog_file=CATALOG
    )
    with open_db(settings) as conn:
        migrate(conn)
        inbox = conn.execute("SELECT id FROM locations WHERE is_system = 1").fetchone()[0]
        service.create_capture(
            conn,
            capture_type="photo",
            now=datetime.now(UTC),
            location_id=inbox,
            uploads=_save(settings, make_jpegs(tmp_path, 2)),
        )
        assert worker.run_once(conn, settings, _TimingOut())
        (call,) = conn.execute("SELECT * FROM ai_calls").fetchall()
    # 2 images without history: 300 + 2 x 1,200 input, the 16,384 photo output cap.
    assert (call["input_tokens"], call["output_tokens"]) == (2_700, 16_384)
    assert call["cost_micros"] == round(2_700 * 0.5 + 16_384 * 2.0)
    assert call["image_count"] == 2 and call["outcome"] == "timeout"


def test_usage_rows_without_image_counts_still_feed_the_estimate(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    now = datetime.now(UTC)
    budget.record_ai_call(
        conn, settings, None, now, Usage(input_tokens=2_300, output_tokens=500), "ok"
    )
    assert budget.token_estimate(conn, 2) == (300 + 2 * 2_000, 100 + 2 * 400, True)


def test_settings_picker_estimates_a_one_photo_capture(make_settings: SettingsFactory) -> None:
    settings = make_settings(
        household_password=TEST_PASSWORD, openrouter_catalog_file=CATALOG, ai_model="test/model-a"
    )
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            usage = Usage(input_tokens=6_300, output_tokens=1_300)  # 3 photos
            budget.record_ai_call(
                conn, settings, None, datetime.now(UTC), usage, "ok", image_count=3
            )
        html = client.get("/settings/models", params={"q": "model-a"}).text
    # Per image (6,300 - 300) / 3 = 2,000 in; one photo: 300 + 2,000 in, 100 + 400 out.
    assert "Per 1-photo capture: about 2,300 input +" in html
    assert "500 output tokens (average" in html


def test_report_numbers_only_boxed_lines_and_skips_pdf_receipt_photos(
    make_settings: SettingsFactory, tmp_path: Path
) -> None:
    """A line gets its box number only when it has boxes; a PDF receipt case has no
    displayable photo, so it gets no Boxes strip (no broken image)."""
    settings = make_settings(
        household_password=TEST_PASSWORD, openrouter_catalog_file=CATALOG, ai_model="test/model-a"
    )
    with logged_in(settings) as client:
        with open_db(settings) as conn:
            root, _ext = group_with_extension(conn, settings, tmp_path)
            photo_case = cases.save(conn, root, [Expected("Tape"), Expected("Saw")])
            pdf = done_capture(
                conn,
                settings,
                "receipt",
                extract("Milk", price=199),
                upload=FIXTURES / "receipt.pdf",
            )
            pdf_case = cases.save(conn, pdf, [Expected("Milk")])
            answer = result(
                [item("Tape", boxes=[{"image": 0, "box_2d": [0, 0, 500, 500]}]), item("Saw")]
            )
            fake = FakeProvider(models={"test/model-a": [answer, extract("Milk", price=199)]})
            run_id = runs.start(conn, ["test/model-a"], [photo_case, pdf_case], 0, T0)
            while execution.run_once(conn, settings, fake.with_model):
                pass
            views = {v.case_id: v for v in report.build(conn, settings, run_id).cases}
        html = client.get(f"/compare/runs/{run_id}").text
    assert views[pdf_case].photos == []
    assert len(views[photo_case].photos) == 3
    assert f'id="case-{photo_case}-gallery"' in html
    assert f'id="case-{pdf_case}-gallery"' not in html
    photo_part = html.split(f'id="case-{photo_case}"', 1)[1].split(f'id="case-{pdf_case}"', 1)[0]
    numbers = re.findall(r'<span class="line-num" aria-label="box (\d+)">', photo_part)
    assert numbers == ["1"]  # Tape is boxed; Saw (number 2) has no box: no number
    assert 'class="line-num"' not in html.split(f'id="case-{pdf_case}"', 1)[1]

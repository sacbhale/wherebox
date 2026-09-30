"""Step 6 export: GET /api/export.json and `python -m app.cli export` share one document."""

import hashlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from fastapi.testclient import TestClient

from app import cli
from app.ai.fake import FakeProvider
from app.config import Settings
from app.inventory import items, locations
from app.main import create_app
from tests.capture_helpers import FIXTURES, post_photo, run, script_step
from tests.inventory_helpers import code_of, open_db


def _seed(client: TestClient, settings: Settings) -> dict[str, int]:
    with open_db(settings) as conn:
        garage, _ = locations.create(conn, "Garage")
        shelf, _ = locations.create(conn, "Shelf 2", garage)
        attic, _ = locations.create(conn, "Attic")
        drill = items.create(
            conn,
            items.ItemData(
                name="Drill",
                location_id=shelf,
                kind="belonging",
                category="Tools",
                price_cents=4999,
                notes="cordless",
            ),
        )
        milk = items.create(
            conn, items.ItemData(name="Milk", location_id=garage, kind="consumable", quantity=2)
        )
        items.use_up(conn, milk)
        locations.delete(conn, attic)
    assert post_photo(client, shelf).status_code == 200
    assert run(settings, FakeProvider([script_step(0)]))
    return {"garage": garage, "shelf": shelf, "attic": attic, "drill": drill, "milk": milk}


def _cli_export(settings: Settings, *args: str) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = cli.main(["export", *args], settings)
    return code, out.getvalue()


def test_export_contents(auth_client: TestClient, auth_settings: Settings) -> None:
    ids = _seed(auth_client, auth_settings)
    response = auth_client.get("/api/export.json")
    assert response.status_code == 200
    doc = response.json()
    assert doc["version"] == 1
    assert set(doc) == {"version", "locations", "items", "photos"}

    locs = {row["id"]: row for row in doc["locations"]}
    with open_db(auth_settings) as conn:
        for key in ("garage", "shelf", "attic"):
            assert locs[ids[key]]["short_code"] == code_of(conn, ids[key])
    assert locs[ids["shelf"]]["parent_id"] == ids["garage"]
    assert locs[ids["attic"]]["deleted"] is True
    assert locs[ids["attic"]]["deleted_at"]
    assert locs[ids["garage"]]["deleted"] is False
    inbox = [row for row in doc["locations"] if row["is_system"] is True]
    assert [row["name"] for row in inbox] == ["Inbox"]
    assert set(locs[ids["garage"]]) >= {
        "id",
        "short_code",
        "name",
        "parent_id",
        "is_system",
        "deleted",
        "deleted_at",
        "created_at",
    }

    by_id = {row["id"]: row for row in doc["items"]}
    drill = by_id[ids["drill"]]
    assert drill["name"] == "Drill"
    assert drill["category"] == "Tools"
    assert drill["price_cents"] == 4999
    assert drill["notes"] == "cordless"
    assert drill["location_code"] == locs[ids["shelf"]]["short_code"]
    assert drill["deleted"] is False
    assert by_id[ids["milk"]]["deleted"] is True
    assert by_id[ids["milk"]]["deleted_at"]
    captured = [row for row in doc["items"] if row["photo_id"] is not None]
    assert len(captured) == 12
    assert {row["needs_review"] for row in captured} == {True, False}

    [photo] = doc["photos"]
    assert photo["rel_path"].startswith("photos/")
    assert photo["sha256"] == hashlib.sha256((FIXTURES / "shelf.jpg").read_bytes()).hexdigest()
    assert "display_rel_path" in photo
    assert {row["photo_id"] for row in captured} == {photo["id"]}

    for key in ("locations", "items", "photos"):
        assert [row["id"] for row in doc[key]] == sorted(row["id"] for row in doc[key])


def test_api_and_cli_documents_are_identical_and_deterministic(
    auth_client: TestClient, auth_settings: Settings, tmp_path: Path
) -> None:
    _seed(auth_client, auth_settings)
    first = auth_client.get("/api/export.json")
    second = auth_client.get("/api/export.json")
    assert first.content == second.content

    code, stdout = _cli_export(auth_settings)
    assert code == 0
    assert json.loads(stdout) == first.json()
    assert _cli_export(auth_settings)[1] == stdout

    out = tmp_path / "export.json"
    code, stdout = _cli_export(auth_settings, "--out", str(out))
    assert code == 0
    assert stdout == ""
    assert json.loads(out.read_text("utf-8")) == first.json()


def test_export_requires_auth(auth_settings: Settings) -> None:
    with TestClient(create_app(auth_settings)) as anonymous:
        response = anonymous.get("/api/export.json", follow_redirects=False)
    assert response.status_code == 401
    assert "locations" not in response.text


def test_cli_export_without_database_fails(settings: Settings) -> None:
    code, stdout = _cli_export(settings)
    assert code == 1
    assert stdout == ""
    assert not settings.db_path.exists()

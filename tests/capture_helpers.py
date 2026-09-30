"""Shared helpers for capture tests (import as ``from tests.capture_helpers import ...``)."""

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.ai.fake import FakeProvider, parse_step
from app.ai.schemas import ExtractedItem, ExtractResult, ReceiptMeta, Usage
from app.capture import worker
from app.config import Settings
from app.db.connection import connect
from tests.inventory_helpers import HX

FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = FIXTURES / "fake_script.json"
CAPTURE_ID = re.compile(r'id="capture-(\d+)"')


def script_step(index: int) -> ExtractResult:
    """Step ``index`` (0 shelf, 1 receipt, 2 text) of tests/fixtures/fake_script.json."""
    data = json.loads(SCRIPT.read_text("utf-8"))
    outcome = parse_step(data["steps"][index]).outcome
    assert isinstance(outcome, ExtractResult)
    return outcome


def item(name: str, confidence: float = 0.9, **fields: object) -> ExtractedItem:
    values: dict[str, object] = {"quantity": 1, "kind": "consumable", "category": "Misc"}
    values.update(fields)
    return ExtractedItem(name=name, confidence=confidence, **values)  # type: ignore[arg-type]


def result(
    items: list[ExtractedItem],
    receipt: ReceiptMeta | None = None,
    input_tokens: int = 100,
    output_tokens: int = 10,
) -> ExtractResult:
    return ExtractResult(
        items=items,
        receipt=receipt,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def capture_id(response: httpx.Response) -> int:
    match = CAPTURE_ID.search(response.text)
    assert match, response.text
    return int(match.group(1))


def post_photo(
    client: TestClient,
    location_id: int,
    path: Path = FIXTURES / "shelf.jpg",
    content_type: str = "image/jpeg",
) -> httpx.Response:
    return client.post(
        "/captures/photo",
        headers=HX,
        data={"location_id": str(location_id)},
        files={"photo": (path.name, path.read_bytes(), content_type)},
    )


def post_receipt(client: TestClient, path: Path = FIXTURES / "receipt.jpg") -> httpx.Response:
    kind = "application/pdf" if path.suffix == ".pdf" else "image/jpeg"
    return client.post(
        "/captures/receipt", headers=HX, files={"receipt": (path.name, path.read_bytes(), kind)}
    )


def post_text(client: TestClient, text: str, location_id: int | None = None) -> httpx.Response:
    data = {"text": text}
    if location_id is not None:
        data["location_id"] = str(location_id)
    return client.post("/captures/text", headers=HX, data=data)


def run(settings: Settings, provider: FakeProvider | None, now: datetime | None = None) -> bool:
    with closing(connect(settings.db_path)) as conn:
        return worker.run_once(conn, settings, provider, now=now)


def capture_row(conn: sqlite3.Connection, cid: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM captures WHERE id = ?", (cid,)).fetchone()


def capture_items(conn: sqlite3.Connection, cid: int) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM items WHERE capture_id = ? ORDER BY id", (cid,)).fetchall()


def ai_calls(conn: sqlite3.Connection, cid: int | None = None) -> list[sqlite3.Row]:
    if cid is None:
        return conn.execute("SELECT * FROM ai_calls ORDER BY id").fetchall()
    return conn.execute(
        "SELECT * FROM ai_calls WHERE capture_id = ? ORDER BY id", (cid,)
    ).fetchall()

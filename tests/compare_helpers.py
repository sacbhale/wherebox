"""Shared helpers for model-comparison tests (import as ``from tests.compare_helpers import
...``): finished captures made through the real worker, cases, and a per-model fake."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.ai.fake import FakeProvider
from app.ai.schemas import ExtractedItem, ExtractResult, ReceiptMeta, Usage
from app.capture import service, worker
from app.compare import cases
from app.compare.scoring import Expected
from app.config import Settings
from tests.capture_helpers import FIXTURES, item

CATALOG = FIXTURES / "openrouter" / "models.json"
T0 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)


def extract(
    *names: str | tuple[str, int], tokens: int = 100, price: int | None = None
) -> ExtractResult:
    """A fake extraction result: names, or (name, quantity) pairs."""
    items: list[ExtractedItem] = []
    for entry in names:
        name, quantity = (entry, 1) if isinstance(entry, str) else entry
        items.append(item(name, quantity=quantity, price_cents=price))
    return ExtractResult(
        items=items,
        receipt=ReceiptMeta(merchant="Shop") if price is not None else None,
        usage=Usage(input_tokens=tokens, output_tokens=tokens // 10),
    )


def done_capture(
    conn: sqlite3.Connection,
    settings: Settings,
    capture_type: str,
    result: ExtractResult,
    *,
    text: str | None = None,
    upload: Path | None = None,
    model: str = "test/model-a",
    now: datetime = T0,
) -> int:
    """Queue a capture and run it through the real worker with a fake answering ``result``
    as ``model``. Photo/receipt captures store ``upload`` (default shelf/receipt fixture)."""
    saved = None
    if capture_type in ("photo", "receipt", "reconcile"):
        path = upload or FIXTURES / ("receipt.jpg" if capture_type == "receipt" else "shelf.jpg")
        with path.open("rb") as fh:
            saved = service.save_upload(settings, fh, path.name, now)
    location_id = None
    if capture_type in ("photo", "reconcile"):
        location_id = conn.execute("SELECT id FROM locations WHERE is_system = 1").fetchone()[0]
    cid = service.create_capture(
        conn,
        capture_type=capture_type,
        now=now,
        location_id=location_id,
        input_text=text if capture_type == "text" else None,
        upload=saved,
    )
    fake = FakeProvider([result], model=model)
    assert worker.run_once(conn, settings, fake, now=now)
    return cid


def make_case(
    conn: sqlite3.Connection,
    settings: Settings,
    names: list[str | tuple[str, int]],
    *,
    capture_type: str = "text",
    text: str = "some things",
    model: str = "test/model-a",
) -> int:
    """A finished capture of ``capture_type`` plus its case expecting ``names``."""
    expected = [Expected(n, 1) if isinstance(n, str) else Expected(n[0], n[1]) for n in names]
    cid = done_capture(conn, settings, capture_type, extract(*names), text=text, model=model)
    return cases.save(conn, cid, expected)

"""Data for the comparison report page (read-only): the ranked per-model summary and the
per-case drill-down (expected items next to each model's items, marked matched / missed /
extra), plus which model produced each case's original capture. The expected items are the
run's snapshot (``comparison_results.expected_json``), not the case as edited since.

Boxes (shown, not scored): each predicted line carries ``number`` (its 1-based prediction
index, which labels its boxes) and ``boxes`` (fractions with ``photo_id``); "missed" lines
are unnumbered. ``CaseView.photos`` are the case's displayable photos in order."""

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from app.ai import openrouter_catalog
from app.ai.model_choice import effective_model, model_selectable
from app.capture.boxes import displayable
from app.capture.groups import GroupPhoto
from app.compare import cases, runs, scoring
from app.config import Settings


@dataclass(frozen=True)
class Line:
    name: str
    quantity: int
    price_cents: int | None
    mark: str  # matched, extra or missed
    other: str | None = None  # matched: the expected name it matched
    quantity_ok: bool = True
    number: int | None = None  # predicted lines: 1-based prediction index (box labels)
    # predicted lines: [{"image", "photo_id", "x0", "y0", "x1", "y1"}] (fractions)
    boxes: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ModelCase:
    model: str
    status: str
    error: str | None
    f1: float | None
    lines: list[Line]
    error_detail: str | None = None  # the provider's own message, when it differs


@dataclass
class CaseView:
    case: cases.Case | None
    case_id: int
    expected: list[dict[str, Any]]
    models: list[ModelCase] = field(default_factory=list)
    photos: list[GroupPhoto] = field(default_factory=list)  # the case's photos, in order


@dataclass(frozen=True)
class Report:
    run: sqlite3.Row
    models: list[str]
    summaries: list[scoring.ModelSummary]
    indicative: bool
    cases: list[CaseView]
    progress: dict[str, int]
    names: dict[str, str]  # model id -> catalog name
    usable: set[str]  # models "Use this model" may select
    active_model: str
    selectable: bool
    has_receipts: bool  # show the price accuracy column
    original_models: set[str]  # models that made the cases' original captures

    @property
    def finished(self) -> bool:
        return self.run["status"] in runs.FINISHED

    @property
    def n_cases(self) -> int:
        return len(self.cases)


def _lines(snapshot: list[dict[str, Any]], row: sqlite3.Row) -> tuple[list[Line], float | None]:
    items = json.loads(row["items_json"] or "[]")
    scores = json.loads(row["scores_json"] or "null")
    if scores is None:
        return [], None
    matched = {pi: ei for ei, pi in scores["pairs"]}
    lines = []
    for index, item in enumerate(items):
        ei = matched.get(index)
        marks = {"number": index + 1, "boxes": list(item.get("boxes") or [])}
        if ei is None:
            lines.append(
                Line(item["name"], item["quantity"], item.get("price_cents"), "extra", **marks)
            )
        else:
            exp = snapshot[ei]
            lines.append(
                Line(
                    item["name"],
                    item["quantity"],
                    item.get("price_cents"),
                    "matched",
                    other=exp["name"],
                    quantity_ok=exp["quantity"] == item["quantity"],
                    **marks,
                )
            )
    for ei in scores["missed"]:
        exp = snapshot[ei]
        lines.append(Line(exp["name"], exp["quantity"], exp.get("price_cents"), "missed"))
    return lines, scores["f1"]


def case_photos(conn: sqlite3.Connection, case: cases.Case | None) -> list[GroupPhoto]:
    """The case's photos a browser can show (display size, order) for drawing each model's
    boxes; [] for a PDF receipt case (no display image), so its Boxes strip is left out."""
    if case is None or not case.photo_ids:
        return []
    marks = ",".join("?" * len(case.photo_ids))
    rows = {
        r["id"]: r
        for r in conn.execute(
            "SELECT id, capture_id, position, width, height, rel_path, display_rel_path"
            f" FROM photos WHERE id IN ({marks})",
            case.photo_ids,
        )
    }
    shown = [
        rows[pid]
        for pid in case.photo_ids
        if pid in rows and displayable(rows[pid]["rel_path"], rows[pid]["display_rel_path"])
    ]
    return [
        GroupPhoto(r["id"], r["capture_id"], r["position"], i, r["width"], r["height"])
        for i, r in enumerate(shown)
    ]


def _result_score(row: sqlite3.Row) -> scoring.ResultScore:
    return scoring.ResultScore(
        row["model"],
        row["status"],
        json.loads(row["scores_json"] or "null"),
        row["cost_micros"],
        row["duration_ms"],
        len(json.loads(row["expected_json"])),
    )


def ranking(conn: sqlite3.Connection, run: sqlite3.Row) -> list[scoring.ModelSummary]:
    """The ranked per-model summary alone (the finished status shows its top model)."""
    rows = runs.results(conn, run["id"])
    return scoring.summarize(runs.models_of(run), [_result_score(row) for row in rows])


def build(conn: sqlite3.Connection, settings: Settings, run_id: int) -> Report | None:
    run = runs.get(conn, run_id)
    if run is None:
        return None
    models = runs.models_of(run)
    case_ids = runs.case_ids_of(run)
    by_id = cases.many(conn, settings, case_ids)
    rows = runs.results(conn, run_id)
    # Every result of a case carries the same snapshot, taken when the run started.
    views = {
        case_id: CaseView(
            by_id.get(case_id), case_id, [], photos=case_photos(conn, by_id.get(case_id))
        )
        for case_id in case_ids
    }
    order = {model: i for i, model in enumerate(models)}
    scored: list[scoring.ResultScore] = []
    for row in sorted(rows, key=lambda r: (r["case_id"], order.get(r["model"], 99))):
        snapshot = json.loads(row["expected_json"])
        scored.append(_result_score(row))
        view = views.get(row["case_id"])
        if view is not None:
            view.expected = snapshot
            lines, f1 = _lines(snapshot, row)
            view.models.append(
                ModelCase(row["model"], row["status"], row["error"], f1, lines, row["error_detail"])
            )
    summaries = scoring.summarize(models, scored)
    catalog = openrouter_catalog.load_catalog(settings)
    infos = {m: catalog.get(m) for m in models}
    names = {m: info.name if info else m for m, info in infos.items()}
    # Like Settings: without a catalog a well-formed id is accepted.
    usable = {
        m for m, info in infos.items() if (info.price_known if info else not catalog.available)
    }
    return Report(
        run=run,
        models=models,
        summaries=summaries,
        indicative=scoring.indicative(summaries),
        cases=[views[case_id] for case_id in case_ids],
        progress=runs.progress(conn, run_id),
        names=names,
        usable=usable,
        active_model=effective_model(conn, settings),
        selectable=model_selectable(settings),
        has_receipts=any(case.type == "receipt" for case in by_id.values()),
        original_models={case.capture_model for case in by_id.values() if case.capture_model},
    )

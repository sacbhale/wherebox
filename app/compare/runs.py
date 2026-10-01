"""Comparison runs and results repository (``comparison_runs``, ``comparison_results``).

State machine (spec R4): a run is ``running`` from creation, then ``done`` (no result
pending), ``failed`` (budget) or ``cancelled``; every transition out of ``running`` is
conditional on the run still being ``running``. A result is ``pending`` until its call is
recorded as ``ok``/``failed``, or the run is cancelled/failed. Result updates after a call are
conditional on ``status = 'pending'``, so a cancel that lands mid-call wins. Nothing is ever
deleted.
"""

import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.ai.provider import bounded
from app.capture.budget import month_window
from app.config import Settings
from app.db.connection import write_tx
from app.db.timestamps import utc_iso

FINISHED = ("done", "failed", "cancelled")
BUDGET_REACHED = "monthly budget reached"


class RunConflict(Exception):
    """Another comparison run is still running."""


def start(
    conn: sqlite3.Connection,
    models: Sequence[str],
    case_ids: Sequence[int],
    estimate_micros: int,
    now: datetime,
) -> int:
    """Create a running run and one pending result per (case, model), in one write_tx that
    also checks no other run is running (so a double submit cannot create two runs) and
    snapshots each case's expected items into its results (a case edited during the run
    cannot score the models differently). Results are created case by case, so the models
    progress side by side."""
    stamp = utc_iso(now)
    with write_tx(conn):
        if conn.execute("SELECT 1 FROM comparison_runs WHERE status = 'running'").fetchone():
            raise RunConflict("A comparison is already running.")
        run_id = conn.execute(
            "INSERT INTO comparison_runs (status, models_json, case_ids_json, estimate_micros,"
            " created_at) VALUES ('running', ?, ?, ?, ?)",
            (json.dumps(list(models)), json.dumps(list(case_ids)), estimate_micros, stamp),
        ).lastrowid
        conn.executemany(
            "INSERT INTO comparison_results (run_id, model, case_id, not_before, expected_json)"
            " VALUES (?, ?, ?, ?, (SELECT expected_json FROM benchmark_cases WHERE id = ?))",
            [(run_id, model, case_id, stamp, case_id) for case_id in case_ids for model in models],
        )
    assert run_id is not None
    return run_id


def cancel(conn: sqlite3.Connection, run_id: int, now: datetime) -> bool:
    """Cancel a running run and its pending results. False when it is not running."""
    with write_tx(conn):
        cursor = conn.execute(
            "UPDATE comparison_runs SET status = 'cancelled', finished_at = ?"
            " WHERE id = ? AND status = 'running'",
            (utc_iso(now), run_id),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            "UPDATE comparison_results SET status = 'cancelled'"
            " WHERE run_id = ? AND status = 'pending'",
            (run_id,),
        )
    return True


def fail_run(conn: sqlite3.Connection, run_id: int, error: str, now: datetime) -> bool:
    """Mark a running run failed and its pending results failed with ``error``."""
    with write_tx(conn):
        cursor = conn.execute(
            "UPDATE comparison_runs SET status = 'failed', error = ?, finished_at = ?"
            " WHERE id = ? AND status = 'running'",
            (error, utc_iso(now), run_id),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            "UPDATE comparison_results SET status = 'failed', error = ?"
            " WHERE run_id = ? AND status = 'pending'",
            (error, run_id),
        )
    return True


def complete_if_done(conn: sqlite3.Connection, run_id: int, now: datetime) -> bool:
    """Call inside write_tx: a running run without pending results becomes done."""
    cursor = conn.execute(
        "UPDATE comparison_runs SET status = 'done', finished_at = ?"
        " WHERE id = ? AND status = 'running' AND NOT EXISTS"
        " (SELECT 1 FROM comparison_results WHERE run_id = ? AND status = 'pending')",
        (utc_iso(now), run_id, run_id),
    )
    return cursor.rowcount == 1


def complete_finished(conn: sqlite3.Connection, now: datetime) -> None:
    """Close running runs that have nothing pending (e.g. after a crash between the last
    result and the run's completion)."""
    rows = conn.execute("SELECT id FROM comparison_runs WHERE status = 'running'").fetchall()
    if not rows:
        return
    with write_tx(conn):
        for row in rows:
            complete_if_done(conn, row["id"], now)


def claim(conn: sqlite3.Connection, now: datetime) -> sqlite3.Row | None:
    """Take the oldest pending result of the running run whose not_before has passed,
    counting the attempt. The result stays 'pending' (restart simply re-claims it)."""
    with write_tx(conn):
        row = conn.execute(
            "UPDATE comparison_results SET attempts = attempts + 1 WHERE id = ("
            " SELECT r.id FROM comparison_results r"
            " JOIN comparison_runs u ON u.id = r.run_id"
            " WHERE u.status = 'running' AND r.status = 'pending' AND r.not_before <= ?"
            " ORDER BY r.id LIMIT 1) RETURNING id",
            (utc_iso(now),),
        ).fetchone()
    if row is None:
        return None
    return conn.execute("SELECT * FROM comparison_results WHERE id = ?", (row[0],)).fetchone()


def store_outcome(
    conn: sqlite3.Connection,
    result_id: int,
    *,
    status: str,
    now: datetime,
    usage: tuple[int, int, int] = (0, 0, 0),
    duration_ms: int | None = None,
    error: str | None = None,
    error_detail: str | None = None,
    items: list[dict[str, Any]] | None = None,
    scores: dict[str, Any] | None = None,
    retry_after: timedelta | None = None,
) -> bool:
    """Call inside write_tx: record one attempt of a pending result. ``usage`` is (input
    tokens, output tokens, cost micros) and adds to earlier attempts. ``status`` 'pending'
    with ``retry_after`` schedules another attempt. ``error`` is the plain-language message,
    ``error_detail`` the provider's own one when it differs (both stored on one line, at most
    MAX_ERROR_CHARS). False when the result was no longer pending (cancelled meanwhile):
    nothing is changed then."""
    if status not in ("ok", "failed", "pending"):
        raise ValueError(f"unexpected result status {status!r}")
    not_before = utc_iso(now + (retry_after or timedelta()))
    cursor = conn.execute(
        "UPDATE comparison_results SET status = ?, error = ?, error_detail = ?,"
        " input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,"
        " cost_micros = cost_micros + ?, duration_ms = coalesce(?, duration_ms),"
        " items_json = ?, scores_json = ?,"
        " not_before = CASE WHEN ? = 'pending' THEN ? ELSE not_before END"
        " WHERE id = ? AND status = 'pending'",
        (
            status,
            None if error is None else bounded(error),
            None if error_detail is None else bounded(error_detail),
            *usage,
            duration_ms,
            None if items is None else json.dumps(items),
            None if scores is None else json.dumps(scores),
            status,
            not_before,
            result_id,
        ),
    )
    return cursor.rowcount == 1


# ---------------------------------------------------------------- reads


def get(conn: sqlite3.Connection, run_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM comparison_runs WHERE id = ?", (run_id,)).fetchone()


def running(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM comparison_runs WHERE status = 'running' ORDER BY id LIMIT 1"
    ).fetchone()


def recent(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Newest runs first, with result counts and total cost."""
    return conn.execute(
        "SELECT u.*, count(r.id) AS total,"
        " sum(r.status IN ('ok', 'failed', 'cancelled')) AS finished,"
        " coalesce(sum(r.cost_micros), 0) AS cost_micros"
        " FROM comparison_runs u LEFT JOIN comparison_results r ON r.run_id = u.id"
        " GROUP BY u.id ORDER BY u.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def results(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM comparison_results WHERE run_id = ? ORDER BY id", (run_id,)
    ).fetchall()


def progress(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    """Result counts by status (every status present, 0 when none), plus ``total`` and
    ``cost_micros``."""
    counts = {"pending": 0, "ok": 0, "failed": 0, "cancelled": 0}
    for row in conn.execute(
        "SELECT status, count(*) FROM comparison_results WHERE run_id = ? GROUP BY status",
        (run_id,),
    ):
        counts[row[0]] = row[1]
    cost = conn.execute(
        "SELECT coalesce(sum(cost_micros), 0) FROM comparison_results WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]
    return counts | {"total": sum(counts.values()), "cost_micros": cost}


@dataclass(frozen=True)
class ModelProgress:
    """One model's results in a run. ``waiting``: pending results scheduled again after an
    error, the first of them due in ``retry_in_s`` seconds (from ``now``; 0 when due);
    ``error`` / ``error_detail``: its latest error (plain / the provider's own)."""

    model: str
    total: int = 0
    ok: int = 0
    failed: int = 0
    cancelled: int = 0
    waiting: int = 0
    retry_in_s: int | None = None
    error: str | None = None
    error_detail: str | None = None

    @property
    def done(self) -> int:
        return self.ok + self.failed + self.cancelled


def model_progress(
    conn: sqlite3.Connection, run: sqlite3.Row, now: datetime
) -> list[ModelProgress]:
    """Per model, in the run's model order."""
    counts: dict[str, dict[str, Any]] = {model: {} for model in models_of(run)}
    for row in conn.execute(
        "SELECT model, count(*) AS total, sum(status = 'ok') AS ok,"
        " sum(status = 'failed') AS failed, sum(status = 'cancelled') AS cancelled,"
        " sum(status = 'pending' AND error IS NOT NULL) AS waiting,"
        " min(CASE WHEN status = 'pending' AND error IS NOT NULL THEN not_before END) AS due"
        " FROM comparison_results WHERE run_id = ? GROUP BY model",
        (run["id"],),
    ):
        fields = {key: row[key] for key in ("total", "ok", "failed", "cancelled", "waiting")}
        if row["due"] is not None:
            wait = (datetime.fromisoformat(row["due"]) - now).total_seconds()
            fields["retry_in_s"] = max(math.ceil(wait), 0)
        counts.setdefault(row["model"], {}).update(fields)
    for row in conn.execute(
        "SELECT model, error, error_detail FROM comparison_results"
        " WHERE run_id = ? AND error IS NOT NULL ORDER BY id",
        (run["id"],),
    ):
        counts.setdefault(row["model"], {}).update(
            error=row["error"], error_detail=row["error_detail"]
        )
    return [ModelProgress(model, **fields) for model, fields in counts.items()]


def models_of(run: sqlite3.Row) -> list[str]:
    return list(json.loads(run["models_json"]))


def case_ids_of(run: sqlite3.Row) -> list[int]:
    return [int(case_id) for case_id in json.loads(run["case_ids_json"])]


def month_cost_micros(conn: sqlite3.Connection, settings: Settings, now: datetime) -> int:
    """This month's ledger spend on comparison calls (part of the budget total)."""
    start, end = month_window(settings, now)
    return conn.execute(
        "SELECT coalesce(sum(cost_micros), 0) FROM ai_calls"
        " WHERE comparison_result_id IS NOT NULL AND started_at >= ? AND started_at < ?",
        (start, end),
    ).fetchone()[0]

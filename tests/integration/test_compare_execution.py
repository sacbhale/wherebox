"""Comparison execution on the worker thread (spec C2/R4): captures first, one result per
tick, per-model fake scripts, ledger rows with comparison_result_id, budget exhaustion,
failing models, transient retry, rate-limit (429) backoff, cancel mid-call, restart, and no
inventory writes."""

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from app.ai import openrouter_catalog
from app.ai.fake import FakeProvider
from app.ai.provider import (
    ProviderPermanentError,
    ProviderRateLimited,
    ProviderTimeoutError,
    ProviderTransientError,
)
from app.ai.schemas import ExtractRequest, ExtractResult, Usage
from app.capture import budget, service, worker
from app.capture.worker import Worker
from app.compare import cases, execution, report, runs
from app.compare.scoring import Expected
from app.config import Settings
from app.db.connection import connect
from app.db.migrate import migrate
from tests.compare_helpers import CATALOG, extract, make_case
from tests.conftest import SettingsFactory

GOOD = "test/model-b"
CHEAP = "openai/gpt-5-nano"
OTHER = "test/model-a"


@pytest.fixture(autouse=True)
def _fresh_catalog():
    openrouter_catalog.clear_cache()
    yield
    openrouter_catalog.clear_cache()


@pytest.fixture
def settings(make_settings: SettingsFactory) -> Settings:
    return make_settings(openrouter_catalog_file=CATALOG, ai_model="test/model-a")


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _worker(settings: Settings, fake: FakeProvider, clock: Callable[[], datetime]) -> Worker:
    return Worker(settings, provider_for=fake.with_model, clock=clock)


def _drain(worker_: Worker, conn: sqlite3.Connection, limit: int = 50) -> int:
    ticks = 0
    while worker_._tick(conn):
        ticks += 1
        assert ticks < limit, "worker never went idle"
    return ticks


def _counts(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    return tuple(  # type: ignore[return-value]
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("items", "captures", "photos", "review_proposals")
    )


def _three_cases(conn: sqlite3.Connection, settings: Settings) -> list[int]:
    return [
        make_case(conn, settings, ["AA batteries", "Duct tape"], capture_type="photo"),
        make_case(conn, settings, ["Whole milk", "Bread"], capture_type="receipt"),
        make_case(conn, settings, [("Milk", 2), "Cordless drill"], text="two milks and a drill"),
    ]


def _start(conn: sqlite3.Connection, case_ids: list[int], clock: Clock, models=(GOOD, CHEAP)):
    return runs.start(conn, list(models), case_ids, 0, clock())


def _results(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return runs.results(conn, run_id)


def test_modes_match_the_capture_handlers() -> None:
    for capture_type, mode in execution.MODES.items():
        assert worker.HANDLERS[capture_type].mode == mode


def test_two_models_three_cases(conn: sqlite3.Connection, settings: Settings) -> None:
    case_ids = _three_cases(conn, settings)
    before = _counts(conn)
    calls_before = conn.execute("SELECT count(*) FROM ai_calls").fetchone()[0]
    fake = FakeProvider(
        models={
            GOOD: [
                extract("AA batteries", "Duct tape"),
                extract("Whole milk", "Bread", price=250),
                extract(("Milk", 2), "Cordless drill"),
            ],
            CHEAP: [
                extract("Batteries"),
                extract("Whole milk", "Sales tax", "Bag of chips", price=250),
                extract(("Milk", 1), "Drill", "Hammer"),
            ],
        }
    )
    clock = Clock()
    run_id = _start(conn, case_ids, clock)
    ticks = _drain(_worker(settings, fake, clock), conn)

    assert ticks == 6  # one call per tick
    assert runs.get(conn, run_id)["status"] == "done"
    rows = _results(conn, run_id)
    assert [r["status"] for r in rows] == ["ok"] * 6
    assert [r["attempts"] for r in rows] == [1] * 6
    # Inputs prepared like real captures: same modes, images, known locations.
    good = [r for r in fake.requests if r.mode][:6]
    assert [r.mode for r in good] == ["shelf", "shelf", "receipt", "receipt", "text", "text"]
    assert all(len(r.images) == 1 for r in good[:4])
    assert good[4].text == "two milks and a drill" and "Inbox" in good[4].known_locations
    # Ledger: one row per call, linked to its result, not to a capture.
    calls = conn.execute(
        "SELECT * FROM ai_calls WHERE comparison_result_id IS NOT NULL ORDER BY id"
    ).fetchall()
    assert [c["comparison_result_id"] for c in calls] == [r["id"] for r in rows]
    assert all(c["capture_id"] is None and c["outcome"] == "ok" for c in calls)
    assert conn.execute("SELECT count(*) FROM ai_calls").fetchone()[0] == calls_before + 6
    # Receipt predictions are normalized like a capture's apply (denylisted "tax" dropped).
    receipt_cheap = next(r for r in rows if r["model"] == CHEAP and r["case_id"] == case_ids[1])
    assert "Sales tax" not in receipt_cheap["items_json"]
    # Comparisons never touch items, captures, photos or proposals.
    assert _counts(conn) == before

    data = report.build(conn, settings, run_id)
    assert data is not None
    assert [s.model for s in data.summaries] == [GOOD, CHEAP]
    assert data.summaries[0].mean_f1 == 1.0 and data.summaries[1].mean_f1 < 1.0


def test_captures_take_priority(conn: sqlite3.Connection, settings: Settings) -> None:
    case_ids = _three_cases(conn, settings)[:1]
    fake = FakeProvider(
        [extract("Queued capture item")],
        models={GOOD: [extract("AA batteries")], CHEAP: [extract("Duct tape")]},
    )
    clock = Clock()
    run_id = _start(conn, case_ids, clock)
    cid = service.create_capture(conn, capture_type="text", now=clock(), input_text="stuff")
    w = _worker(settings, fake, clock)

    assert w._tick(conn)
    assert conn.execute("SELECT status FROM captures WHERE id = ?", (cid,)).fetchone()[0] == "done"
    assert [r["status"] for r in _results(conn, run_id)] == ["pending", "pending"]
    assert w._tick(conn)
    assert [r["status"] for r in _results(conn, run_id)] == ["ok", "pending"]


def test_failing_model_does_not_stop_the_other(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    case_ids = _three_cases(conn, settings)
    fake = FakeProvider(
        models={
            GOOD: [extract("AA batteries"), extract("Whole milk"), extract("Milk")],
            CHEAP: [ProviderPermanentError("Model does not accept images")] * 3,
        }
    )
    clock = Clock()
    run_id = _start(conn, case_ids, clock)
    _drain(_worker(settings, fake, clock), conn)

    assert runs.get(conn, run_id)["status"] == "done"
    by_model = {(r["model"], r["status"]) for r in _results(conn, run_id)}
    assert by_model == {(GOOD, "ok"), (CHEAP, "failed")}
    failed = [r for r in _results(conn, run_id) if r["model"] == CHEAP]
    assert all(r["error"] == "Model does not accept images" for r in failed)
    outcomes = [
        row[0]
        for row in conn.execute(
            "SELECT outcome FROM ai_calls WHERE comparison_result_id IS NOT NULL"
        )
    ]
    assert sorted(outcomes) == ["ok"] * 3 + ["permanent_error"] * 3
    summaries = report.build(conn, settings, run_id).summaries  # type: ignore[union-attr]
    assert summaries[1].model == CHEAP and summaries[1].failures == 3


def test_transient_error_is_retried_once_later(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    case_ids = _three_cases(conn, settings)[:2]
    fake = FakeProvider(
        models={
            GOOD: [ProviderTransientError("overloaded"), extract("Whole milk"), extract("AA")],
            CHEAP: [
                ProviderTransientError("rate limited"),
                extract("Duct tape"),
                ProviderTransientError("rate limited"),
            ],
        }
    )
    clock = Clock()
    run_id = _start(conn, case_ids, clock)
    w = _worker(settings, fake, clock)

    assert w._tick(conn)
    first = _results(conn, run_id)[0]
    assert (first["status"], first["attempts"], first["error"]) == ("pending", 1, "overloaded")
    assert first["not_before"] > runs.get(conn, run_id)["created_at"]
    _drain(w, conn)  # the other results run; the retried ones wait 30 s
    statuses = [r["status"] for r in _results(conn, run_id)]
    assert statuses.count("pending") == 2
    assert runs.get(conn, run_id)["status"] == "running"

    clock.advance(31)
    _drain(w, conn)
    rows = _results(conn, run_id)
    assert runs.get(conn, run_id)["status"] == "done"
    assert [(r["model"], r["status"], r["attempts"]) for r in rows] == [
        (GOOD, "ok", 2),
        (CHEAP, "failed", 2),  # transient twice: failed
        (GOOD, "ok", 1),
        (CHEAP, "ok", 1),
    ]
    assert rows[1]["error"] == "rate limited"
    ledger = conn.execute(
        "SELECT count(*) FROM ai_calls WHERE comparison_result_id IS NOT NULL"
    ).fetchone()[0]
    assert ledger == 6  # every attempt is one ledger row


def _wait_s(row: sqlite3.Row, clock: Clock) -> int:
    """Seconds from the clock's now until the result may be claimed again (stored times
    have millisecond precision)."""
    return round((datetime.fromisoformat(row["not_before"]) - clock()).total_seconds())


def _limited(retry_after: float | None = None) -> ProviderRateLimited:
    return ProviderRateLimited(
        "OpenRouter HTTP 429: Provider returned error", retry_after=retry_after
    )


def test_rate_limit_is_retried_up_to_four_attempts_with_backoff(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    (case_id,) = _three_cases(conn, settings)[2:]
    fake = FakeProvider(
        models={
            # 429 twice (the first with Retry-After 90 s), then an answer on attempt 3.
            GOOD: [_limited(90), _limited(), extract(("Milk", 2), "Cordless drill")],
            # 429 on all 4 attempts (Retry-After 5 s is shorter than the pause: ignored).
            CHEAP: [_limited(), _limited(), _limited(5), _limited()],
        }
    )
    clock = Clock()
    run_id = _start(conn, [case_id], clock)
    w = _worker(settings, fake, clock)

    assert w._tick(conn) and w._tick(conn)
    good, cheap = _results(conn, run_id)
    assert (good["status"], good["attempts"], _wait_s(good, clock)) == ("pending", 1, 90)
    assert (cheap["status"], cheap["attempts"], _wait_s(cheap, clock)) == ("pending", 1, 30)
    assert good["error"] == execution.RATE_LIMITED_WAITING
    assert good["error_detail"] == "OpenRouter HTTP 429: Provider returned error"
    assert not w._tick(conn)  # both wait; nothing else to do

    clock.advance(30)
    assert w._tick(conn) and not w._tick(conn)
    cheap = _results(conn, run_id)[1]
    assert (cheap["attempts"], _wait_s(cheap, clock)) == (2, 60)  # second pause: 60 s

    clock.advance(60)  # 90 s: both are due
    assert w._tick(conn) and w._tick(conn) and not w._tick(conn)
    good, cheap = _results(conn, run_id)
    assert (good["attempts"], _wait_s(good, clock)) == (2, 60)
    assert (cheap["attempts"], _wait_s(cheap, clock)) == (3, 120)  # third pause: 120 s

    clock.advance(60)
    assert w._tick(conn) and not w._tick(conn)
    good = _results(conn, run_id)[0]
    assert (good["status"], good["attempts"], good["error"]) == ("ok", 3, None)
    assert good["error_detail"] is None
    assert json.loads(good["scores_json"])["f1"] == 1.0

    clock.advance(60)
    assert w._tick(conn) and not w._tick(conn)
    cheap = _results(conn, run_id)[1]
    assert (cheap["status"], cheap["attempts"]) == ("failed", 4)
    assert cheap["error"] == (
        "Rate-limited by the model's provider — busy right now; retried 3 times."
    )
    assert cheap["error_detail"] == "OpenRouter HTTP 429: Provider returned error"
    assert runs.get(conn, run_id)["status"] == "done"
    outcomes = [row["outcome"] for row in _ledger(conn)]
    assert outcomes.count("transient_error") == 6 and outcomes.count("ok") == 1
    assert len(fake.requests) == 7


def test_retry_after_lengthens_the_single_transient_retry(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    (case_id,) = _three_cases(conn, settings)[2:]
    busy = ProviderTransientError("OpenRouter HTTP 503: busy", retry_after=100)
    huge = ProviderTransientError("OpenRouter HTTP 503: busy", retry_after=86_400)
    fake = FakeProvider(models={GOOD: [busy, busy], CHEAP: [huge]})
    clock = Clock()
    run_id = _start(conn, [case_id], clock)
    w = _worker(settings, fake, clock)
    assert w._tick(conn) and w._tick(conn)
    good, cheap = _results(conn, run_id)
    assert (good["error"], _wait_s(good, clock)) == ("OpenRouter HTTP 503: busy", 100)
    assert _wait_s(cheap, clock) == execution.MAX_RETRY_AFTER.total_seconds()  # capped
    clock.advance(100)
    assert w._tick(conn)
    good = _results(conn, run_id)[0]
    assert (good["status"], good["attempts"]) == ("failed", 2)  # still only one retry


@pytest.mark.parametrize("asked", [1e14, 1e300, float("inf"), float("nan")])
def test_huge_or_bogus_retry_after_cannot_overflow(
    conn: sqlite3.Connection, settings: Settings, asked: float
) -> None:
    (case_id,) = _three_cases(conn, settings)[2:]
    fake = FakeProvider(
        models={
            GOOD: [_limited(asked)],
            CHEAP: [ProviderTransientError("OpenRouter HTTP 503: busy", retry_after=asked)],
        }
    )
    clock = Clock()
    run_id = _start(conn, [case_id], clock)
    w = _worker(settings, fake, clock)
    assert w._tick(conn) and w._tick(conn)
    good, cheap = _results(conn, run_id)
    capped = execution.MAX_RETRY_AFTER.total_seconds()
    bogus = asked != asked  # nan: no usable Retry-After (inf counts as the cap)
    assert (good["status"], _wait_s(good, clock)) == ("pending", 30 if bogus else capped)
    assert (cheap["status"], _wait_s(cheap, clock)) == ("pending", 30 if bogus else capped)
    assert "internal error" not in (good["error"] + cheap["error"])


def test_every_failure_keeps_the_providers_own_message(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    (case_id,) = _three_cases(conn, settings)[2:]
    raw = "OpenRouter HTTP 404: No endpoints found that can handle the requested parameters."
    exhausted = ProviderPermanentError(
        f"No provider for {GOOD} supports structured output right now", detail=raw
    )
    long_raw = "OpenRouter HTTP 400:\n" + "x " * 2000
    fake = FakeProvider(
        models={
            GOOD: [exhausted],
            CHEAP: [ProviderPermanentError(long_raw)],
            OTHER: [ProviderPermanentError("Model gone", detail=long_raw)],
        }
    )
    clock = Clock()
    run_id = _start(conn, [case_id], clock, models=(GOOD, CHEAP, OTHER))
    _drain(_worker(settings, fake, clock), conn)
    good, cheap, other = _results(conn, run_id)
    assert other["error"] == "Model gone"
    assert len(other["error_detail"]) <= 500 and "\n" not in other["error_detail"]
    assert good["status"] == "failed" and good["error"] == str(exhausted)
    assert good["error_detail"] == raw
    # The plain message is the provider's own: no separate detail; stored bounded.
    assert cheap["error_detail"] is None
    assert len(cheap["error"]) <= 500 and "\n" not in cheap["error"]


def test_budget_exhaustion_mid_run_fails_the_rest(
    make_settings: SettingsFactory,
) -> None:
    settings = make_settings(
        openrouter_catalog_file=CATALOG,
        monthly_ai_budget=1,
        ai_input_price_per_mtok=0,
        ai_output_price_per_mtok=0,
    )
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        case_ids = [make_case(conn, settings, ["Milk"]), make_case(conn, settings, ["Eggs"])]
        costly = ExtractResult(items=[], usage=Usage(input_tokens=10, cost_usd=0.6))
        fake = FakeProvider(models={GOOD: [costly] * 2, CHEAP: [costly] * 2})
        clock = Clock()
        run_id = _start(conn, case_ids, clock)
        _drain(_worker(settings, fake, clock), conn)

        run = runs.get(conn, run_id)
        assert (run["status"], run["error"]) == ("failed", "monthly budget reached")
        rows = _results(conn, run_id)
        assert [r["status"] for r in rows] == ["ok", "ok", "failed", "failed"]
        assert rows[2]["error"] == "monthly budget reached"
        assert len(fake.requests) == 2  # no call after the budget was reached
    finally:
        conn.close()


def test_cancel_landing_mid_call_wins_and_the_call_is_still_recorded(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    case_ids = _three_cases(conn, settings)[:1]
    clock = Clock()
    run_id = _start(conn, case_ids, clock)

    class CancelDuringCall:
        name = "fake"

        def __init__(self, model: str) -> None:
            self.model = model

        def extract(self, req: ExtractRequest) -> ExtractResult:
            other = connect(settings.db_path)
            try:
                assert runs.cancel(other, run_id, clock())
            finally:
                other.close()
            return extract("AA batteries", tokens=500)

    assert execution.run_once(conn, settings, CancelDuringCall, clock=clock)
    run = runs.get(conn, run_id)
    assert run["status"] == "cancelled"  # not flipped to done
    rows = _results(conn, run_id)
    assert [r["status"] for r in rows] == ["cancelled", "cancelled"]
    (call,) = conn.execute(
        "SELECT * FROM ai_calls WHERE comparison_result_id IS NOT NULL"
    ).fetchall()
    assert call["comparison_result_id"] == rows[0]["id"] and call["input_tokens"] == 500
    assert not execution.run_once(conn, settings, CancelDuringCall, clock=clock)


def test_restart_resumes_a_running_run(conn: sqlite3.Connection, settings: Settings) -> None:
    case_ids = _three_cases(conn, settings)[:2]
    fake = FakeProvider(repeat_last=True, models={GOOD: [extract("AA")], CHEAP: [extract("AA")]})
    clock = Clock()
    run_id = _start(conn, case_ids, clock)
    assert _worker(settings, fake, clock)._tick(conn)
    # A crash after a claim leaves the result pending with its attempt counted.
    claimed = runs.claim(conn, clock())
    assert claimed is not None and claimed["status"] == "pending"

    _drain(_worker(settings, fake, clock), conn)  # a new worker after the restart
    rows = _results(conn, run_id)
    assert runs.get(conn, run_id)["status"] == "done"
    assert all(r["status"] == "ok" for r in rows)
    assert rows[1]["attempts"] == 2  # re-claimed once after the "crash"


def test_crash_looping_result_gives_up(conn: sqlite3.Connection, settings: Settings) -> None:
    case_ids = _three_cases(conn, settings)[:1]
    clock = Clock()
    run_id = _start(conn, case_ids, clock, models=(GOOD, CHEAP))
    for _ in range(execution.GIVE_UP_AFTER):
        runs.claim(conn, clock())  # claimed and lost, again and again
    fake = FakeProvider(repeat_last=True, models={GOOD: [extract("AA")], CHEAP: [extract("AA")]})
    _drain(_worker(settings, fake, clock), conn)
    rows = _results(conn, run_id)
    assert rows[0]["status"] == "failed" and rows[0]["error"].startswith("gave up after")
    assert rows[1]["status"] == "ok"


def test_missing_input_and_missing_provider_fail_without_a_call(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    photo_case, _, text_case = _three_cases(conn, settings)
    rel = conn.execute(
        "SELECT p.rel_path FROM photos p JOIN benchmark_cases b ON b.capture_id = p.capture_id"
        " WHERE b.id = ?",
        (photo_case,),
    ).fetchone()[0]
    clock = Clock()
    run_id = _start(conn, [photo_case, text_case], clock)
    (settings.data_dir / rel).unlink()
    calls_before = conn.execute("SELECT count(*) FROM ai_calls").fetchone()[0]

    fake = FakeProvider(repeat_last=True, models={GOOD: [extract("Milk")]})
    provider_for = {GOOD: fake.with_model(GOOD), CHEAP: None}.get
    while execution.run_once(conn, settings, provider_for, clock=clock):
        pass
    rows = _results(conn, run_id)
    assert [(r["status"], r["error"]) for r in rows] == [
        ("failed", "input missing"),
        ("failed", "input missing"),
        ("ok", None),
        ("failed", execution.NOT_CONFIGURED),
    ]
    assert conn.execute("SELECT count(*) FROM ai_calls").fetchone()[0] == calls_before + 1
    assert runs.get(conn, run_id)["status"] == "done"


def test_openrouter_cost_falls_back_to_catalog_prices(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    case_ids = _three_cases(conn, settings)[2:]

    class NoCostOpenRouter:
        name = "openrouter"

        def __init__(self, model: str) -> None:
            self.model = model

        def extract(self, req: ExtractRequest) -> ExtractResult:
            return ExtractResult(items=[], usage=Usage(input_tokens=1_000, output_tokens=100))

    clock = Clock()
    run_id = runs.start(conn, ["test/model-a", GOOD], case_ids, 0, clock())
    while execution.run_once(conn, settings, NoCostOpenRouter, clock=clock):
        pass
    rows = _results(conn, run_id)
    # model-a $0.50/$2.00 per 1M: 700 micros; model-b $1/$4: 1400 micros.
    assert [r["cost_micros"] for r in rows] == [700, 1400]
    ledger = [
        row[0]
        for row in conn.execute(
            "SELECT cost_micros FROM ai_calls WHERE comparison_result_id IS NOT NULL ORDER BY id"
        )
    ]
    assert ledger == [700, 1400]


# ---------------------------------------------------------------- review fixes


class _Stub:
    """A provider stand-in named like OpenRouter, answering ``answer`` (or raising it)."""

    name = "openrouter"
    max_tokens = 4096

    def __init__(self, model: str, answer: ExtractResult | Exception) -> None:
        self.model = model
        self.answer = answer

    def extract(self, req: ExtractRequest) -> ExtractResult:
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _ledger(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ai_calls WHERE comparison_result_id IS NOT NULL ORDER BY id"
    ).fetchall()


@pytest.mark.parametrize("broken", ["scoring", "catalog"])
def test_failure_after_the_call_still_writes_the_ledger_row(
    conn: sqlite3.Connection,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    broken: str,
) -> None:
    case_ids = _three_cases(conn, settings)[2:]
    clock = Clock()
    run_id = _start(conn, case_ids, clock, models=(GOOD,))

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    if broken == "scoring":
        monkeypatch.setattr(execution, "score", boom)
        answer = extract("Milk", tokens=400)
        answer = ExtractResult(
            items=answer.items, usage=Usage(input_tokens=400, output_tokens=40, cost_usd=0.002)
        )
    else:  # no reported cost, and the catalog lookup for the fallback price blows up
        monkeypatch.setattr(openrouter_catalog, "get_model", boom)
        answer = ExtractResult(items=[], usage=Usage(input_tokens=400, output_tokens=40))
    assert execution.run_once(conn, settings, lambda model: _Stub(model, answer), clock=clock)

    (row,) = _results(conn, run_id)
    (call,) = _ledger(conn)
    assert call["comparison_result_id"] == row["id"] and call["outcome"] == "ok"
    assert call["input_tokens"] == 400
    if broken == "scoring":
        assert row["status"] == "failed"
        assert row["error"] == "internal error: RuntimeError: boom"
        assert call["cost_micros"] == row["cost_micros"] == 2_000
    else:
        assert row["status"] == "ok"
    assert runs.get(conn, run_id)["status"] == "done"


def test_timeouts_are_charged_an_estimate_and_not_retried(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    case_ids = _three_cases(conn, settings)[2:]
    input_estimate = budget.capture_token_estimate(conn)[0]
    clock = Clock()
    run_id = _start(conn, case_ids, clock, models=("test/model-a", GOOD))
    answers: dict[str, Exception] = {
        "test/model-a": ProviderTimeoutError(maybe_billed=True),
        GOOD: ProviderTransientError("connection error: RemoteProtocolError", maybe_billed=True),
    }
    while execution.run_once(conn, settings, lambda m: _Stub(m, answers[m]), clock=clock):
        pass

    timed_out, dropped = _results(conn, run_id)
    assert (timed_out["status"], timed_out["attempts"]) == ("failed", 1)  # no retry
    assert (dropped["status"], dropped["attempts"]) == ("pending", 1)  # retried later
    ledger = _ledger(conn)
    assert [c["outcome"] for c in ledger] == ["timeout", "transient_error"]
    # Conservative: typical input + the whole output cap, at catalog prices
    # (test/model-a $0.50 / $2.00, test/model-b $1 / $4 per 1M tokens).
    assert ledger[0]["cost_micros"] == round(input_estimate * 0.5 + 4096 * 2.0)
    assert ledger[1]["cost_micros"] == round(input_estimate * 1.0 + 4096 * 4.0)
    assert (ledger[0]["input_tokens"], ledger[0]["output_tokens"]) == (input_estimate, 4096)
    assert timed_out["cost_micros"] == ledger[0]["cost_micros"]


def test_a_case_edited_mid_run_is_scored_as_it_was_at_the_start(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    (case_id,) = _three_cases(conn, settings)[2:]  # expects Milk x2 and Cordless drill
    answer = extract(("Milk", 2), "Cordless drill")
    fake = FakeProvider(models={GOOD: [answer], CHEAP: [answer]})
    clock = Clock()
    run_id = _start(conn, [case_id], clock)
    w = _worker(settings, fake, clock)
    assert w._tick(conn)  # GOOD is scored
    cases.update(conn, case_id, [Expected("Hammer"), Expected("Nails"), Expected("Saw")])
    _drain(w, conn)  # CHEAP runs after the edit

    rows = _results(conn, run_id)
    assert [json.loads(r["scores_json"])["f1"] for r in rows] == [1.0, 1.0]
    data = report.build(conn, settings, run_id)
    assert data is not None
    assert [e["name"] for e in data.cases[0].expected] == ["Milk", "Cordless drill"]
    assert [s.mean_f1 for s in data.summaries] == [1.0, 1.0]


def test_report_keeps_each_cases_original_list_after_edits(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    first = make_case(conn, settings, ["Milk", "Bread"])
    second = make_case(conn, settings, ["Hammer", "Nails", "Saw"])
    fake = FakeProvider(
        models={
            GOOD: [extract("Milk", "Bread"), extract("Hammer", "Nails", "Saw")],
            CHEAP: [extract("Milk"), ProviderPermanentError("bad request")],
        }
    )
    clock = Clock()
    run_id = _start(conn, [first, second], clock)
    _drain(_worker(settings, fake, clock), conn)
    # Both cases are edited after the run.
    cases.update(conn, first, [Expected("Eggs")])
    cases.update(conn, second, [Expected("Glue")])

    data = report.build(conn, settings, run_id)
    assert data is not None
    views = {view.case_id: view for view in data.cases}
    assert [e["name"] for e in views[first].expected] == ["Milk", "Bread"]
    assert [e["name"] for e in views[second].expected] == ["Hammer", "Nails", "Saw"]
    cheap = next(s for s in data.summaries if s.model == CHEAP)
    # Bread missed on the first case; all three items of the failed second case.
    assert (cheap.found, cheap.missed, cheap.failures) == (1, 1 + 3, 1)
    cheap_first = next(m for m in views[first].models if m.model == CHEAP)
    assert [(line.name, line.mark) for line in cheap_first.lines] == [
        ("Milk", "matched"),
        ("Bread", "missed"),
    ]

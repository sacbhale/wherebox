-- Story 2 of the OpenRouter feature: model comparison cases, runs and results (new tables
-- only). Nothing is ever deleted from them: cases are soft-deleted (deleted_at) so old
-- reports keep working. ai_calls.comparison_result_id (0003) points at comparison_results.

-- A capture the user corrected, used as a test case. expected_json is a list of
-- {"name": str, "quantity": int, "price_cents": int | null}.
CREATE TABLE benchmark_cases (
    id             INTEGER PRIMARY KEY,
    capture_id     INTEGER NOT NULL UNIQUE REFERENCES captures(id),
    expected_json  TEXT NOT NULL,
    deleted_at     TEXT NULL,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- A run is 'running' from creation until no result is pending (then 'done'), or until it is
-- cancelled or fails (budget). models_json / case_ids_json are JSON lists.
CREATE TABLE comparison_runs (
    id               INTEGER PRIMARY KEY,
    status           TEXT NOT NULL DEFAULT 'running'
                     CHECK (status IN ('running', 'done', 'failed', 'cancelled')),
    models_json      TEXT NOT NULL,
    case_ids_json    TEXT NOT NULL,
    estimate_micros  INTEGER NOT NULL DEFAULT 0,
    error            TEXT NULL,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    finished_at      TEXT NULL
);
CREATE INDEX comparison_runs_status ON comparison_runs(status);

-- One (model, case) call of a run. Tokens, cost and duration add up over its attempts.
-- expected_json is the case's expected list snapshotted when the run started: every model is
-- scored against it, even if the case is edited meanwhile, and reports show it.
CREATE TABLE comparison_results (
    id             INTEGER PRIMARY KEY,
    run_id         INTEGER NOT NULL REFERENCES comparison_runs(id),
    model          TEXT NOT NULL,
    case_id        INTEGER NOT NULL REFERENCES benchmark_cases(id),
    expected_json  TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'ok', 'failed', 'cancelled')),
    attempts       INTEGER NOT NULL DEFAULT 0,
    not_before     TEXT NOT NULL, -- UTC ISO-8601
    items_json     TEXT NULL,
    error          TEXT NULL,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    cost_micros    INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NULL,
    scores_json    TEXT NULL,
    UNIQUE (run_id, model, case_id)
);
CREATE INDEX comparison_results_queue ON comparison_results(run_id, status, not_before);

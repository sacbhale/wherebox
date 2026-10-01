-- Story 1 of the OpenRouter feature: ledger rows written by model comparisons point at their
-- comparison result. The comparison tables come in 0004_model_comparison.sql; SQLite cannot
-- add a REFERENCES constraint to an existing column later, so the link is kept by app code.
ALTER TABLE ai_calls ADD COLUMN comparison_result_id INTEGER NULL;

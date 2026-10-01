-- Comparison results show a plain-language error (error); the provider's own message, when it
-- differs, is kept here for the report's details (never the key: adapters redact it).
ALTER TABLE comparison_results ADD COLUMN error_detail TEXT NULL;

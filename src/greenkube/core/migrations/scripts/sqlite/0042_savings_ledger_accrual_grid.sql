-- 0042: idempotent savings accrual on a fixed UTC grid (roadmap C0.6, C0.7, BUG-32, BUG-33).
-- Ledger rows are no longer compressed or deleted. Accruals are keyed by
-- (recommendation_id, measurement_method, slot_start) and a per-cluster
-- watermark records the end of the last accrued slot so missed slots are
-- backfilled exactly once.
-- Hourly rows written by the removed compression job are kept as
-- legacy_compressed rows. Their raw rows were deleted and part of their
-- value may have been lost, so they are flagged rather than recomputed.
-- Raw rows that still exist are aggregated on read. The old compression
-- deleted the raw rows it rolled up, so the two sets are disjoint. The one
-- exception is a crash between the INSERT and the DELETE of the old Postgres
-- compression, which did not run in a transaction: those raw rows would be
-- counted twice. The savings API reports a legacy_compressed diagnostic for
-- any window that includes legacy rows.

ALTER TABLE recommendation_savings_ledger ADD COLUMN slot_start TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_ledger_slot
    ON recommendation_savings_ledger (recommendation_id, measurement_method, slot_start);

CREATE INDEX IF NOT EXISTS idx_savings_ledger_cluster_period
    ON recommendation_savings_ledger (cluster_name, period_end);

ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN legacy_compressed INTEGER NOT NULL DEFAULT 0;

UPDATE recommendation_savings_ledger_hourly SET legacy_compressed = 1;

-- Migration 0018 back-filled some periods with datetime(), which yields
-- 'YYYY-MM-DD HH:MM:SS'. Normalize them to the ISO-8601 Z form used everywhere
-- else so period comparisons stay lexicographically correct.
UPDATE OR IGNORE recommendation_savings_ledger
SET period_start = strftime('%Y-%m-%dT%H:%M:%SZ', period_start)
WHERE period_start LIKE '____-__-__ __:__:__';

UPDATE OR IGNORE recommendation_savings_ledger
SET period_end = strftime('%Y-%m-%dT%H:%M:%SZ', period_end)
WHERE period_end LIKE '____-__-__ __:__:__';

UPDATE recommendation_savings_ledger_hourly
SET period_start = strftime('%Y-%m-%dT%H:%M:%SZ', period_start)
WHERE period_start LIKE '____-__-__ __:__:__';

UPDATE recommendation_savings_ledger_hourly
SET period_end = strftime('%Y-%m-%dT%H:%M:%SZ', period_end)
WHERE period_end LIKE '____-__-__ __:__:__';

UPDATE recommendation_savings_ledger_hourly
SET period_start = hour_bucket
WHERE period_start IS NULL;

UPDATE recommendation_savings_ledger_hourly
SET period_end = strftime('%Y-%m-%dT%H:%M:%SZ', period_start, '+1 hour')
WHERE period_end IS NULL;

CREATE TABLE IF NOT EXISTS accrual_watermarks (
    cluster_name TEXT NOT NULL,
    stream       TEXT NOT NULL,
    watermark    TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (cluster_name, stream)
);

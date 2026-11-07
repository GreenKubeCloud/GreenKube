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

ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS slot_start TIMESTAMPTZ;

-- Small per-slot amounts are summed over long windows: keep full precision.
ALTER TABLE recommendation_savings_ledger
    ALTER COLUMN co2e_saved_grams TYPE DOUBLE PRECISION,
    ALTER COLUMN cost_saved_dollars TYPE DOUBLE PRECISION;
ALTER TABLE recommendation_savings_ledger_hourly
    ALTER COLUMN co2e_saved_grams TYPE DOUBLE PRECISION,
    ALTER COLUMN cost_saved_dollars TYPE DOUBLE PRECISION;

CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_ledger_slot
    ON recommendation_savings_ledger (recommendation_id, measurement_method, slot_start);

CREATE INDEX IF NOT EXISTS idx_savings_ledger_cluster_period
    ON recommendation_savings_ledger (cluster_name, period_end);

ALTER TABLE recommendation_savings_ledger_hourly
    ADD COLUMN IF NOT EXISTS legacy_compressed BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE recommendation_savings_ledger_hourly SET legacy_compressed = TRUE WHERE legacy_compressed = FALSE;

UPDATE recommendation_savings_ledger_hourly
SET period_start = COALESCE(period_start, hour_bucket),
    period_end = COALESCE(period_end, hour_bucket + INTERVAL '1 hour')
WHERE period_start IS NULL OR period_end IS NULL;

CREATE TABLE IF NOT EXISTS accrual_watermarks (
    cluster_name TEXT        NOT NULL,
    stream       TEXT        NOT NULL,
    watermark    TIMESTAMPTZ NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (cluster_name, stream)
);

-- 0014: Measured savings ledger (PostgreSQL).
-- Distinguishes prorated estimates from post-verification measured savings and
-- allows prior attribution rows to be superseded after a rollback review.
-- The hourly unique key is widened so prorated and measured rows can coexist
-- in the same bucket.

ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS measurement_method TEXT NOT NULL DEFAULT 'prorated';
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS baseline_value DOUBLE PRECISION;
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS actual_value DOUBLE PRECISION;
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN IF NOT EXISTS measurement_method TEXT NOT NULL DEFAULT 'prorated';
ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN IF NOT EXISTS baseline_value DOUBLE PRECISION;
ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN IF NOT EXISTS actual_value DOUBLE PRECISION;
ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN IF NOT EXISTS confidence DOUBLE PRECISION;
ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN IF NOT EXISTS superseded BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE recommendation_savings_ledger_hourly
    DROP CONSTRAINT IF EXISTS recommendation_savings_ledger_hourly_recommendation_id_hour_bucket_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_ledger_hourly_method
    ON recommendation_savings_ledger_hourly (recommendation_id, hour_bucket, measurement_method);

CREATE INDEX IF NOT EXISTS idx_savings_ledger_method
    ON recommendation_savings_ledger (cluster_name, measurement_method);
CREATE INDEX IF NOT EXISTS idx_savings_ledger_superseded
    ON recommendation_savings_ledger (superseded);

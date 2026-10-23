-- 0018: explicit savings periods and idempotent ledger identity.
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS period_start TIMESTAMPTZ;
ALTER TABLE recommendation_savings_ledger ADD COLUMN IF NOT EXISTS period_end TIMESTAMPTZ;
UPDATE recommendation_savings_ledger
SET period_end = timestamp,
    period_start = timestamp - make_interval(secs => period_seconds)
WHERE period_start IS NULL OR period_end IS NULL;

ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN IF NOT EXISTS period_start TIMESTAMPTZ;
ALTER TABLE recommendation_savings_ledger_hourly ADD COLUMN IF NOT EXISTS period_end TIMESTAMPTZ;
UPDATE recommendation_savings_ledger_hourly
SET period_start = hour_bucket,
    period_end = hour_bucket + INTERVAL '1 hour'
WHERE period_start IS NULL OR period_end IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_ledger_period
    ON recommendation_savings_ledger
    (recommendation_id, period_start, period_end, measurement_method);
CREATE UNIQUE INDEX IF NOT EXISTS uq_savings_ledger_hourly_period
    ON recommendation_savings_ledger_hourly
    (recommendation_id, hour_bucket, measurement_method);

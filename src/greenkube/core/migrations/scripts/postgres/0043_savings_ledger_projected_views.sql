-- 0043: relabel prorated accruals as projected and add read-side ledger views
-- (roadmap C0.4, C0.6, BUG-30, BUG-32).
-- savings_accruals_v1 is the single read model of the ledger: raw accrual
-- rows plus the legacy_compressed hourly rows whose raw rows no longer exist.
-- savings_accruals_v1_hourly_rollup aggregates it per UTC hour on read, replacing
-- the destructive hourly compression.
-- Both views are scoped to the v1 ledger on purpose: the name
-- savings_ledger_entries is reserved for the INSERT-only ledger table of the
-- roadmap C6 design, which must be able to coexist with this read model
-- during a dual-write period.

UPDATE recommendation_savings_ledger AS ledger
SET measurement_method = 'projected'
WHERE ledger.measurement_method = 'prorated'
  AND NOT EXISTS (
      SELECT 1
      FROM recommendation_savings_ledger AS existing
      WHERE existing.recommendation_id = ledger.recommendation_id
        AND existing.period_start IS NOT DISTINCT FROM ledger.period_start
        AND existing.period_end IS NOT DISTINCT FROM ledger.period_end
        AND existing.measurement_method = 'projected'
  );

UPDATE recommendation_savings_ledger_hourly AS hourly
SET measurement_method = 'projected'
WHERE hourly.measurement_method = 'prorated'
  AND NOT EXISTS (
      SELECT 1
      FROM recommendation_savings_ledger_hourly AS existing
      WHERE existing.recommendation_id = hourly.recommendation_id
        AND existing.hour_bucket = hourly.hour_bucket
        AND existing.measurement_method = 'projected'
  );

ALTER TABLE recommendation_savings_ledger ALTER COLUMN measurement_method SET DEFAULT 'projected';
ALTER TABLE recommendation_savings_ledger_hourly ALTER COLUMN measurement_method SET DEFAULT 'projected';

DROP VIEW IF EXISTS savings_accruals_v1_hourly_rollup;
DROP VIEW IF EXISTS savings_accruals_v1;

CREATE VIEW savings_accruals_v1 AS
SELECT
    'raw'::TEXT AS source,
    recommendation_id,
    cluster_name,
    namespace,
    recommendation_type,
    CASE
        WHEN measurement_method IS NULL OR measurement_method = 'prorated' THEN 'projected'
        ELSE measurement_method
    END AS measurement_method,
    co2e_saved_grams::DOUBLE PRECISION AS co2e_saved_grams,
    cost_saved_dollars::DOUBLE PRECISION AS cost_saved_dollars,
    period_start,
    period_end,
    slot_start,
    superseded,
    FALSE AS legacy_compressed
FROM recommendation_savings_ledger
UNION ALL
SELECT
    'legacy_compressed'::TEXT AS source,
    recommendation_id,
    cluster_name,
    namespace,
    recommendation_type,
    CASE
        WHEN measurement_method IS NULL OR measurement_method = 'prorated' THEN 'projected'
        ELSE measurement_method
    END AS measurement_method,
    co2e_saved_grams::DOUBLE PRECISION AS co2e_saved_grams,
    cost_saved_dollars::DOUBLE PRECISION AS cost_saved_dollars,
    period_start,
    period_end,
    NULL::TIMESTAMPTZ AS slot_start,
    superseded,
    TRUE AS legacy_compressed
FROM recommendation_savings_ledger_hourly
WHERE legacy_compressed;

CREATE VIEW savings_accruals_v1_hourly_rollup AS
SELECT
    cluster_name,
    namespace,
    recommendation_id,
    recommendation_type,
    measurement_method,
    (date_trunc('hour', period_start AT TIME ZONE 'UTC') AT TIME ZONE 'UTC') AS hour_bucket,
    SUM(co2e_saved_grams) AS co2e_saved_grams,
    SUM(cost_saved_dollars) AS cost_saved_dollars,
    COUNT(*) AS entry_count,
    BOOL_OR(legacy_compressed) AS legacy_compressed
FROM savings_accruals_v1
WHERE NOT superseded
GROUP BY
    cluster_name,
    namespace,
    recommendation_id,
    recommendation_type,
    measurement_method,
    (date_trunc('hour', period_start AT TIME ZONE 'UTC') AT TIME ZONE 'UTC');

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

UPDATE OR IGNORE recommendation_savings_ledger
SET measurement_method = 'projected'
WHERE measurement_method = 'prorated';

UPDATE OR IGNORE recommendation_savings_ledger_hourly
SET measurement_method = 'projected'
WHERE measurement_method = 'prorated';

DROP VIEW IF EXISTS savings_accruals_v1_hourly_rollup;

DROP VIEW IF EXISTS savings_accruals_v1;

CREATE VIEW savings_accruals_v1 AS
SELECT
    'raw' AS source,
    recommendation_id,
    cluster_name,
    namespace,
    recommendation_type,
    CASE
        WHEN measurement_method IS NULL OR measurement_method = 'prorated' THEN 'projected'
        ELSE measurement_method
    END AS measurement_method,
    co2e_saved_grams,
    cost_saved_dollars,
    period_start,
    period_end,
    slot_start,
    superseded,
    0 AS legacy_compressed
FROM recommendation_savings_ledger
UNION ALL
SELECT
    'legacy_compressed' AS source,
    recommendation_id,
    cluster_name,
    namespace,
    recommendation_type,
    CASE
        WHEN measurement_method IS NULL OR measurement_method = 'prorated' THEN 'projected'
        ELSE measurement_method
    END AS measurement_method,
    co2e_saved_grams,
    cost_saved_dollars,
    period_start,
    period_end,
    NULL AS slot_start,
    superseded,
    1 AS legacy_compressed
FROM recommendation_savings_ledger_hourly
WHERE legacy_compressed = 1;

CREATE VIEW savings_accruals_v1_hourly_rollup AS
SELECT
    cluster_name,
    namespace,
    recommendation_id,
    recommendation_type,
    measurement_method,
    strftime('%Y-%m-%dT%H:00:00Z', period_start) AS hour_bucket,
    SUM(co2e_saved_grams) AS co2e_saved_grams,
    SUM(cost_saved_dollars) AS cost_saved_dollars,
    COUNT(*) AS entry_count,
    MAX(legacy_compressed) AS legacy_compressed
FROM savings_accruals_v1
WHERE superseded = 0
GROUP BY
    cluster_name,
    namespace,
    recommendation_id,
    recommendation_type,
    measurement_method,
    strftime('%Y-%m-%dT%H:00:00Z', period_start);

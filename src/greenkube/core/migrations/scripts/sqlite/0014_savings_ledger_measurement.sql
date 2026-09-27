-- 0014: Measured savings ledger (SQLite).
-- Distinguishes prorated estimates from post-verification measured savings and
-- allows prior attribution rows to be superseded after a rollback review.
-- The hourly unique key is widened so prorated and measured rows can coexist
-- in the same bucket.

ALTER TABLE recommendation_savings_ledger ADD COLUMN measurement_method TEXT NOT NULL DEFAULT 'prorated';
ALTER TABLE recommendation_savings_ledger ADD COLUMN baseline_value REAL;
ALTER TABLE recommendation_savings_ledger ADD COLUMN actual_value REAL;
ALTER TABLE recommendation_savings_ledger ADD COLUMN confidence REAL;
ALTER TABLE recommendation_savings_ledger ADD COLUMN superseded INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS recommendation_savings_ledger_hourly_v2 (
    id                   INTEGER      PRIMARY KEY AUTOINCREMENT,
    recommendation_id    INTEGER      NOT NULL,
    cluster_name         TEXT         NOT NULL DEFAULT '',
    namespace            TEXT         NOT NULL DEFAULT '',
    recommendation_type  TEXT         NOT NULL,
    co2e_saved_grams     REAL         NOT NULL DEFAULT 0.0,
    cost_saved_dollars   REAL         NOT NULL DEFAULT 0.0,
    sample_count         INTEGER      NOT NULL DEFAULT 1,
    hour_bucket          TEXT         NOT NULL,
    measurement_method   TEXT         NOT NULL DEFAULT 'prorated',
    baseline_value       REAL,
    actual_value         REAL,
    confidence           REAL,
    superseded           INTEGER      NOT NULL DEFAULT 0,
    UNIQUE (recommendation_id, hour_bucket, measurement_method)
);

INSERT OR IGNORE INTO recommendation_savings_ledger_hourly_v2
    (recommendation_id, cluster_name, namespace, recommendation_type,
     co2e_saved_grams, cost_saved_dollars, sample_count, hour_bucket,
     measurement_method, baseline_value, actual_value, confidence, superseded)
SELECT recommendation_id, cluster_name, namespace, recommendation_type,
       co2e_saved_grams, cost_saved_dollars, sample_count, hour_bucket,
       'prorated', NULL, NULL, NULL, 0
FROM recommendation_savings_ledger_hourly;

DROP TABLE recommendation_savings_ledger_hourly;
ALTER TABLE recommendation_savings_ledger_hourly_v2 RENAME TO recommendation_savings_ledger_hourly;

CREATE INDEX IF NOT EXISTS idx_savings_ledger_hourly_cluster
    ON recommendation_savings_ledger_hourly (cluster_name, hour_bucket);
CREATE INDEX IF NOT EXISTS idx_savings_ledger_method
    ON recommendation_savings_ledger (cluster_name, measurement_method);
CREATE INDEX IF NOT EXISTS idx_savings_ledger_superseded
    ON recommendation_savings_ledger (superseded);

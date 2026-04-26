-- 0008_recommendation_savings_ledger.sql
-- Time-series ledger for prorated savings attributed to applied recommendations.
-- Raw 5-min records + hourly aggregates mirror the combined_metrics pattern.

CREATE TABLE IF NOT EXISTS recommendation_savings_ledger (
    id                   SERIAL PRIMARY KEY,
    recommendation_id    INTEGER      NOT NULL,
    cluster_name         TEXT         NOT NULL DEFAULT '',
    namespace            TEXT         NOT NULL DEFAULT '',
    recommendation_type  TEXT         NOT NULL,
    co2e_saved_grams     REAL         NOT NULL DEFAULT 0.0,
    cost_saved_dollars   REAL         NOT NULL DEFAULT 0.0,
    period_seconds       INTEGER      NOT NULL DEFAULT 300,
    timestamp            TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_savings_ledger_timestamp
    ON recommendation_savings_ledger (timestamp);

CREATE INDEX IF NOT EXISTS idx_savings_ledger_cluster_type
    ON recommendation_savings_ledger (cluster_name, recommendation_type);

CREATE TABLE IF NOT EXISTS recommendation_savings_ledger_hourly (
    id                   SERIAL PRIMARY KEY,
    recommendation_id    INTEGER      NOT NULL,
    cluster_name         TEXT         NOT NULL DEFAULT '',
    namespace            TEXT         NOT NULL DEFAULT '',
    recommendation_type  TEXT         NOT NULL,
    co2e_saved_grams     REAL         NOT NULL DEFAULT 0.0,
    cost_saved_dollars   REAL         NOT NULL DEFAULT 0.0,
    sample_count         INTEGER      NOT NULL DEFAULT 1,
    hour_bucket          TIMESTAMPTZ  NOT NULL,
    UNIQUE (recommendation_id, hour_bucket)
);

CREATE INDEX IF NOT EXISTS idx_savings_ledger_hourly_cluster
    ON recommendation_savings_ledger_hourly (cluster_name, hour_bucket);

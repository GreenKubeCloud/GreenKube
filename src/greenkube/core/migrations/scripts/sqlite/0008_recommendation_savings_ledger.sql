-- 0008_recommendation_savings_ledger.sql (SQLite)
CREATE TABLE IF NOT EXISTS recommendation_savings_ledger (
    id                   INTEGER      PRIMARY KEY AUTOINCREMENT,
    recommendation_id    INTEGER      NOT NULL,
    cluster_name         TEXT         NOT NULL DEFAULT '',
    namespace            TEXT         NOT NULL DEFAULT '',
    recommendation_type  TEXT         NOT NULL,
    co2e_saved_grams     REAL         NOT NULL DEFAULT 0.0,
    cost_saved_dollars   REAL         NOT NULL DEFAULT 0.0,
    period_seconds       INTEGER      NOT NULL DEFAULT 300,
    timestamp            TEXT         NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_savings_ledger_timestamp
    ON recommendation_savings_ledger (timestamp);

CREATE TABLE IF NOT EXISTS recommendation_savings_ledger_hourly (
    id                   INTEGER      PRIMARY KEY AUTOINCREMENT,
    recommendation_id    INTEGER      NOT NULL,
    cluster_name         TEXT         NOT NULL DEFAULT '',
    namespace            TEXT         NOT NULL DEFAULT '',
    recommendation_type  TEXT         NOT NULL,
    co2e_saved_grams     REAL         NOT NULL DEFAULT 0.0,
    cost_saved_dollars   REAL         NOT NULL DEFAULT 0.0,
    sample_count         INTEGER      NOT NULL DEFAULT 1,
    hour_bucket          TEXT         NOT NULL,
    UNIQUE (recommendation_id, hour_bucket)
);

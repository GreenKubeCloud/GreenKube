-- 0041: Immutable verification measurements (C0.13, BUG-37).
-- One row per decided fixed-horizon measurement of an applied recommendation.
-- Rows are insert-only: re-measurement creates a new row that supersedes the old one.
CREATE TABLE IF NOT EXISTS savings_measurements (
    measurement_id TEXT PRIMARY KEY,
    recommendation_id INTEGER NOT NULL,
    group_id TEXT NOT NULL,
    group_recommendation_ids TEXT NOT NULL DEFAULT '[]',
    plan_id TEXT,
    change_id TEXT,
    sequence INTEGER NOT NULL DEFAULT 1,
    strategy_key TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    impact_model_version TEXT,
    baseline_snapshot_id TEXT,
    scope TEXT NOT NULL,
    windows TEXT NOT NULL,
    estimates TEXT NOT NULL,
    health TEXT NOT NULL DEFAULT '[]',
    confounders TEXT NOT NULL DEFAULT '[]',
    ledger_input TEXT NOT NULL DEFAULT '{}',
    outcome TEXT NOT NULL,
    reasons TEXT NOT NULL DEFAULT '[]',
    evidence_sha256 TEXT NOT NULL,
    supersedes_measurement_id TEXT,
    decided_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'verifier',
    UNIQUE (recommendation_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_savings_measurements_recommendation
    ON savings_measurements(recommendation_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_savings_measurements_group ON savings_measurements(group_id);
CREATE INDEX IF NOT EXISTS idx_savings_measurements_decided_at ON savings_measurements(decided_at);
CREATE TRIGGER IF NOT EXISTS trg_savings_measurements_no_update
BEFORE UPDATE ON savings_measurements
BEGIN
    SELECT RAISE(ABORT, 'savings_measurements rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_savings_measurements_no_delete
BEFORE DELETE ON savings_measurements
BEGIN
    SELECT RAISE(ABORT, 'savings_measurements rows are immutable');
END;

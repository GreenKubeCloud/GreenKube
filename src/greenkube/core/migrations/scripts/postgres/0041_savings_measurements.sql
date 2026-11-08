-- 0041: Immutable verification measurements (C0.13, BUG-37).
-- One row per decided fixed-horizon measurement of an applied recommendation.
-- Rows are insert-only: re-measurement creates a new row that supersedes the old one.
CREATE TABLE IF NOT EXISTS savings_measurements (
    measurement_id TEXT PRIMARY KEY,
    recommendation_id BIGINT NOT NULL,
    group_id TEXT NOT NULL,
    group_recommendation_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    plan_id TEXT,
    change_id TEXT,
    sequence INTEGER NOT NULL DEFAULT 1,
    strategy_key TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    impact_model_version TEXT,
    baseline_snapshot_id TEXT,
    scope TEXT NOT NULL,
    windows JSONB NOT NULL,
    estimates JSONB NOT NULL,
    health JSONB NOT NULL DEFAULT '[]'::jsonb,
    confounders JSONB NOT NULL DEFAULT '[]'::jsonb,
    ledger_input JSONB NOT NULL DEFAULT '{}'::jsonb,
    outcome TEXT NOT NULL,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_sha256 TEXT NOT NULL,
    supersedes_measurement_id TEXT,
    decided_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'verifier',
    UNIQUE (recommendation_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_savings_measurements_recommendation
    ON savings_measurements(recommendation_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_savings_measurements_group ON savings_measurements(group_id);
CREATE INDEX IF NOT EXISTS idx_savings_measurements_decided_at ON savings_measurements(decided_at);

CREATE OR REPLACE FUNCTION greenkube_reject_savings_measurement_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'savings_measurements rows are immutable';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_savings_measurements_immutable ON savings_measurements;
CREATE TRIGGER trg_savings_measurements_immutable
    BEFORE UPDATE OR DELETE ON savings_measurements
    FOR EACH ROW EXECUTE FUNCTION greenkube_reject_savings_measurement_mutation();

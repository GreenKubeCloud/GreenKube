-- 0056: Change measurement (roadmap C1, C2, C4, C5).
-- change_events, change_members, baseline_snapshots, change_measurements and
-- change_state_events are insert-only (triggers reject UPDATE and DELETE).
-- change_states holds the current lifecycle state of each change; every
-- transition is a compare-and-swap on (state, version) written with its event.
CREATE TABLE IF NOT EXISTS change_events (
    change_id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    plan_id TEXT,
    pr_url TEXT,
    merge_sha TEXT,
    gitops_revision TEXT,
    synced_at TIMESTAMP WITH TIME ZONE,
    namespace TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    workload_name TEXT NOT NULL,
    containers JSONB NOT NULL DEFAULT '[]'::jsonb,
    workload_uid TEXT,
    generation_before BIGINT,
    generation_after BIGINT,
    spec_digest_before TEXT,
    spec_digest_after TEXT,
    rollout_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    rollout_started_source TEXT NOT NULL,
    rollout_completed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    rollout_completed_source TEXT NOT NULL,
    baseline_window_end TIMESTAMP WITH TIME ZONE NOT NULL,
    action_kind TEXT NOT NULL,
    application_method TEXT NOT NULL,
    load_metric TEXT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_events_workload
    ON change_events(namespace, workload_kind, workload_name, rollout_completed_at);
CREATE INDEX IF NOT EXISTS idx_change_events_created ON change_events(created_at, change_id);

CREATE TABLE IF NOT EXISTS change_members (
    change_id TEXT NOT NULL,
    recommendation_id BIGINT NOT NULL,
    added_at TIMESTAMP WITH TIME ZONE NOT NULL,
    PRIMARY KEY (change_id, recommendation_id)
);
CREATE INDEX IF NOT EXISTS idx_change_members_recommendation ON change_members(recommendation_id);

CREATE TABLE IF NOT EXISTS baseline_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    change_id TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL,
    window_start TIMESTAMP WITH TIME ZONE NOT NULL,
    window_end TIMESTAMP WITH TIME ZONE NOT NULL,
    hours JSONB NOT NULL DEFAULT '[]'::jsonb,
    load_series JSONB NOT NULL DEFAULT '{}'::jsonb,
    price_card JSONB NOT NULL DEFAULT '{}'::jsonb,
    intensity_source TEXT,
    impact_model_version TEXT,
    collector_versions JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);

CREATE TABLE IF NOT EXISTS change_measurements (
    measurement_id TEXT PRIMARY KEY,
    change_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    strategy_key TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    impact_model_version TEXT,
    baseline_snapshot_id TEXT,
    baseline_sha256 TEXT,
    windows JSONB NOT NULL,
    load_driver JSONB,
    counterfactual_params JSONB NOT NULL DEFAULT '{}'::jsonb,
    estimates JSONB NOT NULL DEFAULT '[]'::jsonb,
    confounders JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
    health JSONB NOT NULL DEFAULT '[]'::jsonb,
    allocations JSONB NOT NULL DEFAULT '[]'::jsonb,
    outcome TEXT NOT NULL,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    evidence_sha256 TEXT NOT NULL,
    supersedes_measurement_id TEXT,
    decided_at TIMESTAMP WITH TIME ZONE NOT NULL,
    UNIQUE (change_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_change_measurements_decided_at ON change_measurements(decided_at);

CREATE TABLE IF NOT EXISTS change_states (
    change_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    sequence INTEGER NOT NULL DEFAULT 1,
    latest_measurement_id TEXT,
    post_window_start TIMESTAMP WITH TIME ZONE,
    post_window_end TIMESTAMP WITH TIME ZONE,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_states_state ON change_states(state);

CREATE TABLE IF NOT EXISTS change_state_events (
    id BIGSERIAL PRIMARY KEY,
    change_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_state_events_change ON change_state_events(change_id, id);

CREATE OR REPLACE FUNCTION greenkube_reject_change_record_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_change_events_immutable ON change_events;
CREATE TRIGGER trg_change_events_immutable
    BEFORE UPDATE OR DELETE ON change_events
    FOR EACH ROW EXECUTE FUNCTION greenkube_reject_change_record_mutation();
DROP TRIGGER IF EXISTS trg_change_members_immutable ON change_members;
CREATE TRIGGER trg_change_members_immutable
    BEFORE UPDATE OR DELETE ON change_members
    FOR EACH ROW EXECUTE FUNCTION greenkube_reject_change_record_mutation();
DROP TRIGGER IF EXISTS trg_baseline_snapshots_immutable ON baseline_snapshots;
CREATE TRIGGER trg_baseline_snapshots_immutable
    BEFORE UPDATE OR DELETE ON baseline_snapshots
    FOR EACH ROW EXECUTE FUNCTION greenkube_reject_change_record_mutation();
DROP TRIGGER IF EXISTS trg_change_measurements_immutable ON change_measurements;
CREATE TRIGGER trg_change_measurements_immutable
    BEFORE UPDATE OR DELETE ON change_measurements
    FOR EACH ROW EXECUTE FUNCTION greenkube_reject_change_record_mutation();
DROP TRIGGER IF EXISTS trg_change_state_events_immutable ON change_state_events;
CREATE TRIGGER trg_change_state_events_immutable
    BEFORE UPDATE OR DELETE ON change_state_events
    FOR EACH ROW EXECUTE FUNCTION greenkube_reject_change_record_mutation();

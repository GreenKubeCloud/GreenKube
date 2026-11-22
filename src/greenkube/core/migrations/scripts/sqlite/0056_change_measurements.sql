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
    synced_at TEXT,
    namespace TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    workload_name TEXT NOT NULL,
    containers TEXT NOT NULL DEFAULT '[]',
    workload_uid TEXT,
    generation_before INTEGER,
    generation_after INTEGER,
    spec_digest_before TEXT,
    spec_digest_after TEXT,
    rollout_started_at TEXT NOT NULL,
    rollout_started_source TEXT NOT NULL,
    rollout_completed_at TEXT NOT NULL,
    rollout_completed_source TEXT NOT NULL,
    baseline_window_end TEXT NOT NULL,
    action_kind TEXT NOT NULL,
    application_method TEXT NOT NULL,
    load_metric TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_events_workload
    ON change_events(namespace, workload_kind, workload_name, rollout_completed_at);
CREATE INDEX IF NOT EXISTS idx_change_events_created ON change_events(created_at, change_id);
CREATE TRIGGER IF NOT EXISTS trg_change_events_no_update
BEFORE UPDATE ON change_events
BEGIN
    SELECT RAISE(ABORT, 'change_events rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_change_events_no_delete
BEFORE DELETE ON change_events
BEGIN
    SELECT RAISE(ABORT, 'change_events rows are immutable');
END;

CREATE TABLE IF NOT EXISTS change_members (
    change_id TEXT NOT NULL,
    recommendation_id INTEGER NOT NULL,
    added_at TEXT NOT NULL,
    PRIMARY KEY (change_id, recommendation_id)
);
CREATE INDEX IF NOT EXISTS idx_change_members_recommendation ON change_members(recommendation_id);
CREATE TRIGGER IF NOT EXISTS trg_change_members_no_update
BEFORE UPDATE ON change_members
BEGIN
    SELECT RAISE(ABORT, 'change_members rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_change_members_no_delete
BEFORE DELETE ON change_members
BEGIN
    SELECT RAISE(ABORT, 'change_members rows are immutable');
END;

CREATE TABLE IF NOT EXISTS baseline_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    change_id TEXT NOT NULL UNIQUE,
    content_sha256 TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    hours TEXT NOT NULL DEFAULT '[]',
    load_series TEXT NOT NULL DEFAULT '{}',
    price_card TEXT NOT NULL DEFAULT '{}',
    intensity_source TEXT,
    impact_model_version TEXT,
    collector_versions TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_baseline_snapshots_no_update
BEFORE UPDATE ON baseline_snapshots
BEGIN
    SELECT RAISE(ABORT, 'baseline_snapshots rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_baseline_snapshots_no_delete
BEFORE DELETE ON baseline_snapshots
BEGIN
    SELECT RAISE(ABORT, 'baseline_snapshots rows are immutable');
END;

CREATE TABLE IF NOT EXISTS change_measurements (
    measurement_id TEXT PRIMARY KEY,
    change_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    strategy_key TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    impact_model_version TEXT,
    baseline_snapshot_id TEXT,
    baseline_sha256 TEXT,
    windows TEXT NOT NULL,
    load_driver TEXT,
    counterfactual_params TEXT NOT NULL DEFAULT '{}',
    estimates TEXT NOT NULL DEFAULT '[]',
    confounders TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0,
    health TEXT NOT NULL DEFAULT '[]',
    allocations TEXT NOT NULL DEFAULT '[]',
    outcome TEXT NOT NULL,
    reasons TEXT NOT NULL DEFAULT '[]',
    evidence_sha256 TEXT NOT NULL,
    supersedes_measurement_id TEXT,
    decided_at TEXT NOT NULL,
    UNIQUE (change_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_change_measurements_decided_at ON change_measurements(decided_at);
CREATE TRIGGER IF NOT EXISTS trg_change_measurements_no_update
BEFORE UPDATE ON change_measurements
BEGIN
    SELECT RAISE(ABORT, 'change_measurements rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_change_measurements_no_delete
BEFORE DELETE ON change_measurements
BEGIN
    SELECT RAISE(ABORT, 'change_measurements rows are immutable');
END;

CREATE TABLE IF NOT EXISTS change_states (
    change_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    sequence INTEGER NOT NULL DEFAULT 1,
    latest_measurement_id TEXT,
    post_window_start TEXT,
    post_window_end TEXT,
    details TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_states_state ON change_states(state);

CREATE TABLE IF NOT EXISTS change_state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_state_events_change ON change_state_events(change_id, id);
CREATE TRIGGER IF NOT EXISTS trg_change_state_events_no_update
BEFORE UPDATE ON change_state_events
BEGIN
    SELECT RAISE(ABORT, 'change_state_events rows are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_change_state_events_no_delete
BEFORE DELETE ON change_state_events
BEGIN
    SELECT RAISE(ABORT, 'change_state_events rows are immutable');
END;

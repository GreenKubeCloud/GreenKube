-- 0057: Pod-template revisions observed by the applied detector (roadmap C1, C4).
-- One row each time a workload's uid, generation, pod-template digest or HPA
-- digest differs from its previous observation. They give a change its
-- "before" spec digest and let the confounder detector see spec and HPA edits.
CREATE TABLE IF NOT EXISTS workload_spec_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    namespace TEXT NOT NULL,
    workload_kind TEXT NOT NULL,
    workload_name TEXT NOT NULL,
    workload_uid TEXT,
    generation INTEGER,
    spec_digest TEXT,
    hpa_digest TEXT,
    observed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workload_spec_observations_workload
    ON workload_spec_observations(namespace, workload_kind, workload_name, observed_at);
CREATE INDEX IF NOT EXISTS idx_savings_measurements_change ON savings_measurements(change_id);

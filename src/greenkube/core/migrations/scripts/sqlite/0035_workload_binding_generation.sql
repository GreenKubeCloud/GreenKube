-- 0035: add optimistic concurrency generations for workload bindings (SQLite).

ALTER TABLE workload_source_bindings
    ADD COLUMN generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1);

CREATE INDEX IF NOT EXISTS idx_workload_binding_candidates_projection
    ON workload_binding_candidates (binding_id, confidence DESC, source_id);

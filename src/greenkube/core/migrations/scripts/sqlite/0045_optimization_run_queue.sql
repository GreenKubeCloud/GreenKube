-- 0045: optimization runs become the scan request queue (E1) and keep
-- per-source health plus the diff against the previous active set (E8, E10).
-- SQLite cannot drop NOT NULL from started_at, so the table is rebuilt:
-- a queued run has not started yet. Existing rows are copied unchanged and
-- tagged with the 'legacy' trigger.

DROP TABLE IF EXISTS optimization_runs_v45;

CREATE TABLE optimization_runs_v45 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    namespace TEXT,
    trigger_type TEXT NOT NULL DEFAULT 'direct',
    requested_by TEXT,
    requested_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    claimed_by TEXT,
    recommendation_count INTEGER NOT NULL DEFAULT 0,
    analyzer_count INTEGER NOT NULL DEFAULT 0,
    failed_analyzer_count INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER,
    changed_count INTEGER,
    resolved_count INTEGER,
    unchanged_count INTEGER,
    source_outcomes TEXT,
    diff TEXT,
    error TEXT
);

INSERT INTO optimization_runs_v45 (
    id, status, namespace, trigger_type, requested_at, started_at, completed_at,
    recommendation_count, analyzer_count, failed_analyzer_count, error
)
SELECT
    id, status, namespace, 'legacy', started_at, started_at, completed_at,
    recommendation_count, analyzer_count, failed_analyzer_count, error
FROM optimization_runs;

DROP TABLE optimization_runs;

ALTER TABLE optimization_runs_v45 RENAME TO optimization_runs;

CREATE INDEX IF NOT EXISTS idx_optimization_runs_namespace_started
    ON optimization_runs(namespace, started_at);

CREATE INDEX IF NOT EXISTS idx_optimization_runs_status_requested
    ON optimization_runs(status, requested_at, id);

CREATE INDEX IF NOT EXISTS idx_optimization_runs_scope_requested
    ON optimization_runs(namespace, requested_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_optimization_runs_pending_scope
    ON optimization_runs(COALESCE(namespace, '')) WHERE status = 'queued'

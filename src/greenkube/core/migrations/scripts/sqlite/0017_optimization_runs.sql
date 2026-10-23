CREATE TABLE IF NOT EXISTS optimization_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    status TEXT NOT NULL,
    namespace TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    recommendation_count INTEGER NOT NULL DEFAULT 0,
    analyzer_count INTEGER NOT NULL DEFAULT 0,
    failed_analyzer_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_optimization_runs_namespace_started
    ON optimization_runs(namespace, started_at);

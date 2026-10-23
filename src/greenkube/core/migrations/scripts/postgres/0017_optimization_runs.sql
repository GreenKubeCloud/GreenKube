CREATE TABLE IF NOT EXISTS optimization_runs (
    id BIGSERIAL PRIMARY KEY,
    status TEXT NOT NULL,
    namespace TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    recommendation_count INTEGER NOT NULL DEFAULT 0,
    analyzer_count INTEGER NOT NULL DEFAULT 0,
    failed_analyzer_count INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_optimization_runs_namespace_started
    ON optimization_runs(namespace, started_at);

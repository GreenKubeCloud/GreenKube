-- 0065: per-source run history of the Source SDK (roadmap E5).
-- One row per source per optimization scan: status, health, signal and
-- recommendation counts, consecutive failures (circuit breaker), the time of
-- the last real collection (rate limit), an API-safe error code and the
-- source's diagnostics (JSON array). Additive and idempotent.

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    optimization_run_id INTEGER,
    source_id TEXT NOT NULL,
    source_version TEXT,
    namespace TEXT,
    status TEXT NOT NULL,
    complete INTEGER NOT NULL DEFAULT 0,
    health_status TEXT,
    health_reason TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_seconds REAL NOT NULL DEFAULT 0,
    signal_count INTEGER NOT NULL DEFAULT 0,
    recommendation_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_collected_at TEXT,
    error_code TEXT,
    error TEXT,
    diagnostics TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_source_runs_source_started ON source_runs (source_id, started_at, id);
CREATE INDEX IF NOT EXISTS idx_source_runs_started ON source_runs (started_at);
CREATE INDEX IF NOT EXISTS idx_source_runs_optimization_run ON source_runs (optimization_run_id);

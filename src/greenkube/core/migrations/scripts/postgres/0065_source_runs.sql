-- 0065: per-source run history of the Source SDK (roadmap E5).
-- One row per source per optimization scan: status, health, signal and
-- recommendation counts, consecutive failures (circuit breaker), the time of
-- the last real collection (rate limit), an API-safe error code and the
-- source's diagnostics. Additive and idempotent.

CREATE TABLE IF NOT EXISTS source_runs (
    id BIGSERIAL PRIMARY KEY,
    optimization_run_id BIGINT,
    source_id TEXT NOT NULL,
    source_version TEXT,
    namespace TEXT,
    status TEXT NOT NULL,
    complete BOOLEAN NOT NULL DEFAULT FALSE,
    health_status TEXT,
    health_reason TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL,
    duration_seconds DOUBLE PRECISION NOT NULL DEFAULT 0,
    signal_count INTEGER NOT NULL DEFAULT 0,
    recommendation_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_collected_at TIMESTAMPTZ,
    error_code TEXT,
    error TEXT,
    diagnostics JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_source_runs_source_started ON source_runs (source_id, started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_source_runs_started ON source_runs (started_at);
CREATE INDEX IF NOT EXISTS idx_source_runs_optimization_run ON source_runs (optimization_run_id);

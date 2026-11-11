-- 0076: loop hardening (wave 3 follow-ups). Additive.
--
-- Latest request after a rollout: the summary reads the request of the node
-- row holding the hour's newest sample, so a pod that a rollout moved to
-- another node shows its new request at once instead of the hour's maximum.
-- NULL for rows rolled before this version (they keep the maximum).
ALTER TABLE workload_usage_hourly ADD COLUMN IF NOT EXISTS latest_sample_offset_seconds DOUBLE PRECISION;

-- Controller loop heartbeats: each controller replica publishes its loop
-- registry snapshot (leader, readiness, per-loop state) so the API process can
-- report controller loop state. Rows of replicas silent for a day are pruned.
CREATE TABLE IF NOT EXISTS controller_heartbeats (
    instance_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    leader BOOLEAN NOT NULL DEFAULT FALSE,
    ready BOOLEAN NOT NULL DEFAULT FALSE,
    heartbeat_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    loops JSONB NOT NULL DEFAULT '[]'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_controller_heartbeats_heartbeat ON controller_heartbeats (heartbeat_at);

-- 0076: loop hardening (wave 3 follow-ups). Additive.
--
-- Latest request after a rollout: the summary reads the request of the node
-- row holding the hour's newest sample, so a pod that a rollout moved to
-- another node shows its new request at once instead of the hour's maximum.
-- NULL for rows rolled before this version (they keep the maximum).
ALTER TABLE workload_usage_hourly ADD COLUMN latest_sample_offset_seconds REAL;

-- Controller loop heartbeats: each controller replica publishes its loop
-- registry snapshot (leader, readiness, per-loop state) so the API process can
-- report controller loop state. Rows of replicas silent for a day are pruned.
CREATE TABLE IF NOT EXISTS controller_heartbeats (
    instance_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    leader INTEGER NOT NULL DEFAULT 0,
    ready INTEGER NOT NULL DEFAULT 0,
    heartbeat_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    loops TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_controller_heartbeats_heartbeat ON controller_heartbeats (heartbeat_at);

-- 0073: index change events by rollout completion (roadmap C4, C8) (SQLite).
-- The pool confounder check lists the changes that landed during a measurement
-- across the cluster, so it reads change_events by rollout_completed_at alone.
-- Additive.
-- Note: the runner splits scripts on semicolons, so comments must not use them.

CREATE INDEX IF NOT EXISTS idx_change_events_rollout_completed
    ON change_events(rollout_completed_at, change_id);

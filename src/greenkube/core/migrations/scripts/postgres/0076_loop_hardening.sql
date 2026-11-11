-- 0076: loop hardening (wave 3 follow-ups). Additive.
--
-- Latest request after a rollout: the summary reads the request of the node
-- row holding the hour's newest sample, so a pod that a rollout moved to
-- another node shows its new request at once instead of the hour's maximum.
-- NULL for rows rolled before this version (they keep the maximum).
ALTER TABLE workload_usage_hourly ADD COLUMN IF NOT EXISTS latest_sample_offset_seconds DOUBLE PRECISION;

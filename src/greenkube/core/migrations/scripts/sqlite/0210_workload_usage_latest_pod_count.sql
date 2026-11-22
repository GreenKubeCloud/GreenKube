-- 0210: replicas at the hour's latest step (review of F-08). Additive.
--
-- pod_count counts the distinct pods of a node row's hour: a pod replaced by a
-- rollout counts twice, which hid a real singleton. latest_pod_count counts the
-- pods seen at the row's latest sample (within half a step). NULL for rows
-- rolled before this version (pod_count is read instead).
ALTER TABLE workload_usage_hourly ADD COLUMN latest_pod_count INTEGER;

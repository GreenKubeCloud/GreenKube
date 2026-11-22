-- 0130: per-pod restart increase of each workload usage hour (REC-04). Additive.
--
-- The rollup compares each pod's restart count with that pod's previous count
-- (a reset or a new pod adds its whole count) and stores the hour's sum. The
-- lookback restart history used when the scan's OOM and restart signals are
-- unknown is the sum of these values: the highest count across replicas is
-- not a counter. NULL for rows rolled before this version: a lookback that
-- holds such an hour with a restart count has an unknown restart history.
ALTER TABLE workload_usage_hourly ADD COLUMN restart_increase INTEGER;

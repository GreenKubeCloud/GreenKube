-- 0271: Step peak of the pod working set on raw pod rows (COL-12 review).
--
-- memory_usage_max_bytes is the largest working-set scrape inside the step:
-- the pod cgroup peak when only pod-level series exist, else the sum of its
-- container peaks. Rows written before stay NULL (the step value is used).
-- Container rows already have the column (0033).
ALTER TABLE combined_metrics ADD COLUMN memory_usage_max_bytes INTEGER;

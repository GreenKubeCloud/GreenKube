-- 0060: DCGM GPU energy in the hourly workload usage rollup (B5 with E3).
-- gpu_watt_seconds sums gpu_power_watts x seconds over the hour (NULL without
-- GPU data), so the baseline rows of a bounded scan carry the GPU power the
-- Impact Model prices when a workload is deleted or scheduled. Additive.
ALTER TABLE workload_usage_hourly ADD COLUMN gpu_watt_seconds REAL;

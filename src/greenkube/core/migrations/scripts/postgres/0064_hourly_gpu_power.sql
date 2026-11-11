-- 0064: DCGM GPU power in the compressed hourly metric tables (B5 with A3, E3) (PostgreSQL).
-- Migration 0050 added gpu_power_watts to the raw metric tables and 0060 to
-- the workload usage rollup only, so hours compressed before the rollup
-- re-read them lost their GPU power. gpu_power_watts on an hourly row is the
-- duration-weighted average GPU power of the hour (NULL without GPU data),
-- so gpu_power_watts x duration_seconds equals the GPU energy of the raw rows. Additive.
-- Note: the runner splits scripts on semicolons, so comments must not use them.

ALTER TABLE combined_metrics_hourly ADD COLUMN IF NOT EXISTS gpu_power_watts DOUBLE PRECISION;
ALTER TABLE container_metrics_hourly ADD COLUMN IF NOT EXISTS gpu_power_watts DOUBLE PRECISION;

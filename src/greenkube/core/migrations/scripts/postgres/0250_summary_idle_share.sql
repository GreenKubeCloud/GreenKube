-- 0250: Node idle share in the pre-computed dashboard summary (WP-05.3, NRG-02).
--
-- Cluster-wide rows of metrics_summary keep the idle share of their window
-- (idle_co2e_grams, idle_embodied_co2e_grams, idle_cost, idle_energy_joules)
-- and the first instant of the stored idle rows (idle_data_since). NULL idle
-- values mean unknown: a namespace row, no idle row, or pod rows older than
-- the first idle row. Rows written before this migration keep NULL until the
-- next hourly refresh rewrites them.
ALTER TABLE metrics_summary
    ADD COLUMN IF NOT EXISTS idle_co2e_grams DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS idle_embodied_co2e_grams DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS idle_cost DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS idle_energy_joules DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS idle_data_since TIMESTAMPTZ;

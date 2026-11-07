-- 0039: Per-dimension provenance, cost components and container limits (A4, A7, A9, A11).
-- Columns are additive. Legacy rows are backfilled once: cost_source is opencost
-- when a positive cost was stored without a missing-cost estimation reason,
-- usage_source is prometheus when usage was stored, intensity_source is unknown.
-- Container rows inherit their pod row's cost_source and emaps_zone (their cost
-- was allocated from it). Orphan container rows fall back to the same rule.

ALTER TABLE combined_metrics
    ADD COLUMN IF NOT EXISTS cost_cpu DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_ram DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_storage DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_network DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_lb DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_source TEXT,
    ADD COLUMN IF NOT EXISTS usage_source TEXT,
    ADD COLUMN IF NOT EXISTS power_source TEXT,
    ADD COLUMN IF NOT EXISTS intensity_source TEXT;

ALTER TABLE combined_metrics_hourly
    ADD COLUMN IF NOT EXISTS cost_cpu DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_ram DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_storage DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_network DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_lb DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_source TEXT,
    ADD COLUMN IF NOT EXISTS usage_source TEXT,
    ADD COLUMN IF NOT EXISTS power_source TEXT,
    ADD COLUMN IF NOT EXISTS intensity_source TEXT;

ALTER TABLE container_metrics
    ADD COLUMN IF NOT EXISTS cost_cpu DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_ram DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_storage DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_network DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_lb DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_source TEXT,
    ADD COLUMN IF NOT EXISTS usage_source TEXT,
    ADD COLUMN IF NOT EXISTS power_source TEXT,
    ADD COLUMN IF NOT EXISTS intensity_source TEXT,
    ADD COLUMN IF NOT EXISTS emaps_zone TEXT,
    ADD COLUMN IF NOT EXISTS cpu_limit INTEGER,
    ADD COLUMN IF NOT EXISTS memory_limit BIGINT;

ALTER TABLE container_metrics_hourly
    ADD COLUMN IF NOT EXISTS cost_cpu DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_ram DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_storage DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_network DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_lb DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cost_source TEXT,
    ADD COLUMN IF NOT EXISTS usage_source TEXT,
    ADD COLUMN IF NOT EXISTS power_source TEXT,
    ADD COLUMN IF NOT EXISTS intensity_source TEXT,
    ADD COLUMN IF NOT EXISTS emaps_zone TEXT,
    ADD COLUMN IF NOT EXISTS cpu_limit INTEGER,
    ADD COLUMN IF NOT EXISTS memory_limit BIGINT;

UPDATE combined_metrics SET
    cost_source = COALESCE(cost_source, CASE
        WHEN total_cost > 0 AND COALESCE(estimation_reasons, '') NOT LIKE '%No cost data for pod%' THEN 'opencost'
        ELSE 'none'
    END),
    usage_source = COALESCE(usage_source, CASE
        WHEN cpu_usage_millicores IS NOT NULL OR memory_usage_bytes IS NOT NULL THEN 'prometheus'
    END),
    intensity_source = COALESCE(intensity_source, 'unknown')
WHERE cost_source IS NULL OR intensity_source IS NULL;

UPDATE combined_metrics_hourly SET
    cost_source = COALESCE(cost_source, CASE WHEN total_cost > 0 THEN 'opencost' ELSE 'none' END),
    usage_source = COALESCE(usage_source, CASE
        WHEN cpu_usage_avg IS NOT NULL OR memory_usage_avg IS NOT NULL THEN 'prometheus'
    END),
    intensity_source = COALESCE(intensity_source, 'unknown')
WHERE cost_source IS NULL OR intensity_source IS NULL;

UPDATE container_metrics AS cr SET
    cost_source = COALESCE(cr.cost_source, (
        SELECT cm.cost_source
        FROM combined_metrics AS cm
        WHERE cm.namespace = cr.namespace
          AND cm.pod_name = cr.pod_name
          AND cm.timestamp = cr.timestamp
    ), CASE
        WHEN cr.total_cost > 0 AND COALESCE(cr.estimation_reasons, '') NOT LIKE '%No cost data for pod%' THEN 'opencost'
        ELSE 'none'
    END),
    usage_source = COALESCE(cr.usage_source, CASE
        WHEN cr.cpu_usage_millicores IS NOT NULL OR cr.memory_usage_bytes IS NOT NULL THEN 'prometheus'
    END),
    intensity_source = COALESCE(cr.intensity_source, 'unknown'),
    emaps_zone = COALESCE(cr.emaps_zone, (
        SELECT cm.emaps_zone
        FROM combined_metrics AS cm
        WHERE cm.namespace = cr.namespace
          AND cm.pod_name = cr.pod_name
          AND cm.timestamp = cr.timestamp
    ))
WHERE cr.cost_source IS NULL OR cr.intensity_source IS NULL;

UPDATE container_metrics_hourly SET
    cost_source = COALESCE(cost_source, CASE WHEN total_cost > 0 THEN 'opencost' ELSE 'none' END),
    usage_source = COALESCE(usage_source, CASE
        WHEN cpu_usage_avg IS NOT NULL OR memory_usage_avg IS NOT NULL THEN 'prometheus'
    END),
    intensity_source = COALESCE(intensity_source, 'unknown')
WHERE cost_source IS NULL OR intensity_source IS NULL;

-- 0039: Per-dimension provenance, cost components and container limits (A4, A7, A9, A11).
-- Columns are additive. Legacy rows are backfilled once: cost_source is opencost
-- when a positive cost was stored without a missing-cost estimation reason,
-- usage_source is prometheus when usage was stored, intensity_source is unknown.

ALTER TABLE combined_metrics ADD COLUMN cost_cpu REAL;
ALTER TABLE combined_metrics ADD COLUMN cost_ram REAL;
ALTER TABLE combined_metrics ADD COLUMN cost_storage REAL;
ALTER TABLE combined_metrics ADD COLUMN cost_network REAL;
ALTER TABLE combined_metrics ADD COLUMN cost_lb REAL;
ALTER TABLE combined_metrics ADD COLUMN cost_source TEXT;
ALTER TABLE combined_metrics ADD COLUMN usage_source TEXT;
ALTER TABLE combined_metrics ADD COLUMN power_source TEXT;
ALTER TABLE combined_metrics ADD COLUMN intensity_source TEXT;

ALTER TABLE combined_metrics_hourly ADD COLUMN cost_cpu REAL;
ALTER TABLE combined_metrics_hourly ADD COLUMN cost_ram REAL;
ALTER TABLE combined_metrics_hourly ADD COLUMN cost_storage REAL;
ALTER TABLE combined_metrics_hourly ADD COLUMN cost_network REAL;
ALTER TABLE combined_metrics_hourly ADD COLUMN cost_lb REAL;
ALTER TABLE combined_metrics_hourly ADD COLUMN cost_source TEXT;
ALTER TABLE combined_metrics_hourly ADD COLUMN usage_source TEXT;
ALTER TABLE combined_metrics_hourly ADD COLUMN power_source TEXT;
ALTER TABLE combined_metrics_hourly ADD COLUMN intensity_source TEXT;

ALTER TABLE container_metrics ADD COLUMN cost_cpu REAL;
ALTER TABLE container_metrics ADD COLUMN cost_ram REAL;
ALTER TABLE container_metrics ADD COLUMN cost_storage REAL;
ALTER TABLE container_metrics ADD COLUMN cost_network REAL;
ALTER TABLE container_metrics ADD COLUMN cost_lb REAL;
ALTER TABLE container_metrics ADD COLUMN cost_source TEXT;
ALTER TABLE container_metrics ADD COLUMN usage_source TEXT;
ALTER TABLE container_metrics ADD COLUMN power_source TEXT;
ALTER TABLE container_metrics ADD COLUMN intensity_source TEXT;
ALTER TABLE container_metrics ADD COLUMN emaps_zone TEXT;
ALTER TABLE container_metrics ADD COLUMN cpu_limit INTEGER;
ALTER TABLE container_metrics ADD COLUMN memory_limit INTEGER;

ALTER TABLE container_metrics_hourly ADD COLUMN cost_cpu REAL;
ALTER TABLE container_metrics_hourly ADD COLUMN cost_ram REAL;
ALTER TABLE container_metrics_hourly ADD COLUMN cost_storage REAL;
ALTER TABLE container_metrics_hourly ADD COLUMN cost_network REAL;
ALTER TABLE container_metrics_hourly ADD COLUMN cost_lb REAL;
ALTER TABLE container_metrics_hourly ADD COLUMN cost_source TEXT;
ALTER TABLE container_metrics_hourly ADD COLUMN usage_source TEXT;
ALTER TABLE container_metrics_hourly ADD COLUMN power_source TEXT;
ALTER TABLE container_metrics_hourly ADD COLUMN intensity_source TEXT;
ALTER TABLE container_metrics_hourly ADD COLUMN emaps_zone TEXT;
ALTER TABLE container_metrics_hourly ADD COLUMN cpu_limit INTEGER;
ALTER TABLE container_metrics_hourly ADD COLUMN memory_limit INTEGER;

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

UPDATE container_metrics SET
    cost_source = COALESCE(cost_source, CASE
        WHEN total_cost > 0 AND COALESCE(estimation_reasons, '') NOT LIKE '%No cost data for pod%' THEN 'opencost'
        ELSE 'none'
    END),
    usage_source = COALESCE(usage_source, CASE
        WHEN cpu_usage_millicores IS NOT NULL OR memory_usage_bytes IS NOT NULL THEN 'prometheus'
    END),
    intensity_source = COALESCE(intensity_source, 'unknown'),
    emaps_zone = COALESCE(emaps_zone, (
        SELECT cm.emaps_zone
        FROM combined_metrics AS cm
        WHERE cm.namespace = container_metrics.namespace
          AND cm.pod_name = container_metrics.pod_name
          AND cm."timestamp" = container_metrics.timestamp
    ))
WHERE cost_source IS NULL OR intensity_source IS NULL;

UPDATE container_metrics_hourly SET
    cost_source = COALESCE(cost_source, CASE WHEN total_cost > 0 THEN 'opencost' ELSE 'none' END),
    usage_source = COALESCE(usage_source, CASE
        WHEN cpu_usage_avg IS NOT NULL OR memory_usage_avg IS NOT NULL THEN 'prometheus'
    END),
    intensity_source = COALESCE(intensity_source, 'unknown')
WHERE cost_source IS NULL OR intensity_source IS NULL;

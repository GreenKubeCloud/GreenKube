-- Seed a GreenKube 0.3.1 database (schema at migration 0037) for the upgrade test.
--
-- Rows are relative to now() and stay inside every retention and compression
-- window (raw metrics of the last 5 hours), so the upgraded controller keeps
-- them all: the test compares the counts before and after the upgrade.
-- Idempotent: rows carry the "upgrade-test" marker and re-running adds nothing.

-- 40 pods x 60 five-minute samples of pod metrics.
INSERT INTO combined_metrics (
    pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity, joules,
    cpu_request, memory_request, period, timestamp, duration_seconds,
    is_estimated, estimation_reasons, cpu_usage_millicores, memory_usage_bytes,
    owner_kind, owner_name
)
SELECT
    'upgrade-test-' || pod || '-abc12',
    'upgrade-test',
    0.002 + pod * 0.0001,
    0.5 + pod * 0.01,
    1.2,
    55.0,
    1200.0 + pod,
    250,
    268435456,
    '5m',
    date_trunc('minute', now()) - make_interval(mins => 5 * sample),
    300,
    false,
    '',
    120 + pod,
    134217728,
    'Deployment',
    'upgrade-test-' || pod
FROM generate_series(1, 40) AS pod, generate_series(1, 60) AS sample
ON CONFLICT DO NOTHING;

-- Two containers per pod sample.
INSERT INTO container_metrics (
    pod_name, namespace, container_name, total_cost, co2e_grams, pue, grid_intensity,
    joules, cpu_request, memory_request, cpu_usage_millicores, memory_usage_bytes,
    owner_kind, owner_name, timestamp, duration_seconds, is_estimated, estimation_reasons
)
SELECT
    cm.pod_name, cm.namespace, container, cm.total_cost / 2, cm.co2e_grams / 2, cm.pue,
    cm.grid_intensity, cm.joules / 2, cm.cpu_request / 2, cm.memory_request / 2,
    cm.cpu_usage_millicores / 2, cm.memory_usage_bytes / 2, cm.owner_kind, cm.owner_name,
    cm.timestamp, cm.duration_seconds, false, ''
FROM combined_metrics AS cm
CROSS JOIN (VALUES ('app'), ('sidecar')) AS containers(container)
WHERE cm.namespace = 'upgrade-test'
ON CONFLICT DO NOTHING;

-- 20 recommendations, half of them applied, with lifecycle events and savings.
INSERT INTO recommendation_history (
    pod_name, namespace, type, description, reason, priority, potential_savings_cost,
    potential_savings_co2e_grams, current_cpu_request_millicores,
    recommended_cpu_request_millicores, created_at, scope, status, applied_at,
    owner_kind, owner_name, container_name, source, fingerprint
)
SELECT
    NULL,
    'upgrade-test',
    'RIGHTSIZING_CPU',
    'Upgrade test recommendation ' || n,
    'seeded by hack/upgrade/seed-v0.3.1.sql',
    'medium',
    1.5 + n,
    120.0 + n,
    250,
    120,
    now() - make_interval(hours => n),
    'workload',
    CASE WHEN n % 2 = 0 THEN 'applied' ELSE 'active' END,
    CASE WHEN n % 2 = 0 THEN now() - make_interval(mins => 30) END,
    'Deployment',
    'upgrade-test-' || n,
    'app',
    'greenkube',
    'upgrade-test-' || n
FROM generate_series(1, 20) AS n
WHERE NOT EXISTS (
    SELECT 1 FROM recommendation_history WHERE namespace = 'upgrade-test' AND fingerprint = 'upgrade-test-' || n
);

INSERT INTO recommendation_events (recommendation_id, event_type, actor, payload, created_at)
SELECT rh.id, event_type, 'upgrade-test', '{"seed": true}', rh.created_at + offset_interval
FROM recommendation_history AS rh
CROSS JOIN (VALUES ('created', interval '0 minutes'), ('updated', interval '5 minutes')) AS events(event_type, offset_interval)
WHERE rh.namespace = 'upgrade-test'
  AND NOT EXISTS (SELECT 1 FROM recommendation_events WHERE actor = 'upgrade-test');

INSERT INTO recommendation_savings_ledger (
    recommendation_id, cluster_name, namespace, recommendation_type, co2e_saved_grams,
    cost_saved_dollars, period_seconds, timestamp, measurement_method
)
SELECT rh.id, 'upgrade-test', 'upgrade-test', rh.type, 0.4, 0.001, 300,
       date_trunc('minute', now()) - make_interval(mins => 5 * sample), 'prorated'
FROM recommendation_history AS rh
CROSS JOIN generate_series(1, 6) AS sample
WHERE rh.namespace = 'upgrade-test' AND rh.status = 'applied'
  AND NOT EXISTS (SELECT 1 FROM recommendation_savings_ledger WHERE cluster_name = 'upgrade-test');

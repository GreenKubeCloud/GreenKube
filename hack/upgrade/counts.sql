-- Rows seeded by seed-v0.3.1.sql, compared before and after the upgrade.
SELECT 'combined_metrics' AS name, count(*) AS rows FROM combined_metrics WHERE namespace = 'upgrade-test'
UNION ALL
SELECT 'container_metrics', count(*) FROM container_metrics WHERE namespace = 'upgrade-test'
UNION ALL
SELECT 'recommendation_history', count(*) FROM recommendation_history WHERE namespace = 'upgrade-test'
UNION ALL
SELECT 'recommendation_events', count(*) FROM recommendation_events WHERE actor = 'upgrade-test'
UNION ALL
SELECT 'recommendation_savings_ledger', count(*) FROM recommendation_savings_ledger WHERE cluster_name = 'upgrade-test'
ORDER BY 1;

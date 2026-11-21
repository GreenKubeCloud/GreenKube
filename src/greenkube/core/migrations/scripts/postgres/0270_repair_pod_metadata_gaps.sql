-- 0270: Repair stored pod metadata gaps (COL-01 step 6, roadmap D.2).
--
-- Rows written from the Kubernetes state at collection time (metadata_source
-- NULL) or without pod metadata (missing) can carry owner NULL and requests 0
-- while other rows of the same (namespace, pod) know them. Each such row takes
-- the owner and the requests of the nearest earlier row of the same pod that
-- has metadata (the last known values), else of the nearest later one. Only
-- missing fields are filled. Repaired rows get metadata_source = repaired and
-- a model_version suffix +metadata-repaired, so greenkube recompute re-runs
-- their allocation with the repaired requests. Rows of pods that never had
-- metadata are left as they are. Hourly rows are rebuilt from the updated raw
-- rows through the dirty-hour triggers while the raw rows are kept.
-- Container rows only take a missing owner, when every pod row that has one
-- agrees on it.

CREATE TEMP TABLE gk_0270_repair ON COMMIT DROP AS
SELECT t.id AS target_id, s.id AS source_id
  FROM combined_metrics t
  CROSS JOIN LATERAL (
        SELECT k.id FROM combined_metrics k
         WHERE k.namespace = t.namespace AND k.pod_name = t.pod_name AND k.id <> t.id
           AND COALESCE(k.metadata_source, '') NOT IN ('missing', 'repaired')
           AND ((t.owner_kind IS NULL AND k.owner_kind IS NOT NULL AND k.owner_name IS NOT NULL)
                OR (COALESCE(t.cpu_request, 0) = 0 AND COALESCE(t.memory_request, 0) = 0
                    AND (COALESCE(k.cpu_request, 0) > 0 OR COALESCE(k.memory_request, 0) > 0)))
         ORDER BY CASE WHEN k.timestamp <= t.timestamp THEN 0 ELSE 1 END,
                  ABS(EXTRACT(EPOCH FROM (k.timestamp - t.timestamp))),
                  k.id DESC
         LIMIT 1) s
 WHERE COALESCE(t.metadata_source, 'missing') = 'missing'
   AND (t.owner_kind IS NULL OR (COALESCE(t.cpu_request, 0) = 0 AND COALESCE(t.memory_request, 0) = 0));

UPDATE combined_metrics
   SET owner_kind = CASE WHEN combined_metrics.owner_kind IS NULL AND s.owner_name IS NOT NULL
                         THEN s.owner_kind ELSE combined_metrics.owner_kind END,
       owner_name = CASE WHEN combined_metrics.owner_kind IS NULL AND s.owner_kind IS NOT NULL
                         THEN s.owner_name ELSE combined_metrics.owner_name END,
       cpu_request = CASE WHEN COALESCE(combined_metrics.cpu_request, 0) = 0
                               AND COALESCE(combined_metrics.memory_request, 0) = 0
                          THEN COALESCE(s.cpu_request, 0) ELSE combined_metrics.cpu_request END,
       memory_request = CASE WHEN COALESCE(combined_metrics.cpu_request, 0) = 0
                                  AND COALESCE(combined_metrics.memory_request, 0) = 0
                             THEN COALESCE(s.memory_request, 0) ELSE combined_metrics.memory_request END,
       metadata_source = 'repaired',
       model_version = COALESCE(combined_metrics.model_version, '') || '+metadata-repaired'
  FROM gk_0270_repair r
  JOIN combined_metrics s ON s.id = r.source_id
 WHERE combined_metrics.id = r.target_id;

UPDATE container_metrics
   SET owner_kind = s.owner_kind,
       owner_name = s.owner_name
  FROM (SELECT namespace, pod_name, MAX(owner_kind) AS owner_kind, MAX(owner_name) AS owner_name
          FROM combined_metrics
         WHERE owner_kind IS NOT NULL AND owner_name IS NOT NULL
           AND COALESCE(metadata_source, '') <> 'missing'
         GROUP BY namespace, pod_name
        HAVING COUNT(DISTINCT owner_kind || '/' || owner_name) = 1) s
 WHERE container_metrics.owner_kind IS NULL
   AND container_metrics.namespace = s.namespace
   AND container_metrics.pod_name = s.pod_name;

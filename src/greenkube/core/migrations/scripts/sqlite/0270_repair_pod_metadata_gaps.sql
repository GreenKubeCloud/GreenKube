-- 0270: Repair stored pod metadata gaps (COL-01 step 6, roadmap D.2).
--
-- A target is a pod row with neither owner nor requests whose metadata came
-- from the Kubernetes state at collection time (metadata_source NULL) or was
-- missing. Its source is the last earlier row of the same pod that has
-- metadata (the last known values), else the first later one, within one
-- continuous run of rows of that pod: a gap over three steps (the previous
-- row's duration_seconds, 300 s assumed for legacy rows without one, so with a
-- shorter configured step the tolerance stays 15 minutes), or a new
-- pod_snapshots version (spec change or another incarnation of the pod name),
-- ends the run, so values never cross incarnations. A row that has an owner
-- or a request is never a target (a BestEffort pod has requests 0).
-- The target takes the source's owner and requests and says
-- metadata_source = repaired, or repaired_partial when the source had requests
-- but no owner. Its model_version gets the suffix +metadata-repaired, so
-- greenkube recompute re-runs its allocation, and it gets a new row id, so the
-- workload usage rollup re-rolls its hour (in-place updates are not seen by
-- the rollup high-water marks). Hourly metric rows follow through the
-- dirty-hour triggers. Container rows with owner NULL take their pod's owner
-- when all its pod rows that have one agree on it.
-- The work is linear: one grouped scan finds the pods to repair, window
-- functions order their rows once.

-- Columns every current schema has, added for legacy minimal schemas (no-op otherwise).
ALTER TABLE combined_metrics ADD COLUMN owner_kind TEXT;
ALTER TABLE combined_metrics ADD COLUMN owner_name TEXT;
ALTER TABLE combined_metrics ADD COLUMN cpu_request INTEGER;
ALTER TABLE combined_metrics ADD COLUMN memory_request INTEGER;
ALTER TABLE combined_metrics ADD COLUMN metadata_source TEXT;
ALTER TABLE combined_metrics ADD COLUMN model_version TEXT;
ALTER TABLE combined_metrics ADD COLUMN duration_seconds INTEGER;

DROP TABLE IF EXISTS gk_0270_pods;

CREATE TEMP TABLE gk_0270_pods AS
SELECT namespace, pod_name
  FROM combined_metrics
 GROUP BY namespace, pod_name
HAVING MAX(CASE WHEN COALESCE(metadata_source, 'missing') = 'missing' AND owner_kind IS NULL
                     AND COALESCE(cpu_request, 0) = 0 AND COALESCE(memory_request, 0) = 0 THEN 1 ELSE 0 END) = 1
   AND MAX(CASE WHEN COALESCE(metadata_source, '') NOT IN ('missing', 'repaired', 'repaired_partial')
                     AND ((owner_kind IS NOT NULL AND owner_name IS NOT NULL)
                          OR COALESCE(cpu_request, 0) > 0 OR COALESCE(memory_request, 0) > 0) THEN 1 ELSE 0 END) = 1;

DROP TABLE IF EXISTS gk_0270_rows;

CREATE TEMP TABLE gk_0270_rows AS
SELECT row_id, namespace, pod_name, ts, is_source, is_target,
       SUM(is_break) OVER (PARTITION BY namespace, pod_name ORDER BY ts, row_id ROWS UNBOUNDED PRECEDING) AS seg
  FROM (
        SELECT b.*,
               CASE WHEN b.prev_ts IS NULL THEN 1
                    WHEN (b.ts - b.prev_ts) * 86400.0 > 3 * COALESCE(b.prev_duration, 300) THEN 1
                    WHEN EXISTS (
                         SELECT 1 FROM pod_snapshots s
                          WHERE s.namespace = b.namespace AND s.pod_name = b.pod_name
                            AND julianday(s.first_seen) > b.prev_ts AND julianday(s.first_seen) <= b.ts
                            AND EXISTS (SELECT 1 FROM pod_snapshots e
                                         WHERE e.namespace = s.namespace AND e.pod_name = s.pod_name
                                           AND julianday(e.first_seen) < julianday(s.first_seen))) THEN 1
                    ELSE 0 END AS is_break
          FROM (
                SELECT c.rowid AS row_id, c.namespace, c.pod_name, julianday(c."timestamp") AS ts,
                       LAG(julianday(c."timestamp")) OVER w AS prev_ts,
                       LAG(c.duration_seconds) OVER w AS prev_duration,
                       CASE WHEN COALESCE(c.metadata_source, '') NOT IN ('missing', 'repaired', 'repaired_partial')
                                 AND ((c.owner_kind IS NOT NULL AND c.owner_name IS NOT NULL)
                                      OR COALESCE(c.cpu_request, 0) > 0 OR COALESCE(c.memory_request, 0) > 0)
                            THEN 1 ELSE 0 END AS is_source,
                       CASE WHEN COALESCE(c.metadata_source, 'missing') = 'missing' AND c.owner_kind IS NULL
                                 AND COALESCE(c.cpu_request, 0) = 0 AND COALESCE(c.memory_request, 0) = 0
                            THEN 1 ELSE 0 END AS is_target
                  FROM combined_metrics c
                  JOIN gk_0270_pods p ON p.namespace = c.namespace AND p.pod_name = c.pod_name
                WINDOW w AS (PARTITION BY c.namespace, c.pod_name ORDER BY c."timestamp", c.rowid)
               ) b
       ) r;

DROP TABLE IF EXISTS gk_0270_groups;

CREATE TEMP TABLE gk_0270_groups AS
SELECT row_id, namespace, pod_name, seg, is_source, is_target,
       SUM(is_source) OVER (PARTITION BY namespace, pod_name, seg ORDER BY ts, row_id ROWS UNBOUNDED PRECEDING) AS grp
  FROM gk_0270_rows;

DROP TABLE IF EXISTS gk_0270_repair;

CREATE TEMP TABLE gk_0270_repair AS
SELECT t.row_id AS target_id, s.row_id AS source_id
  FROM gk_0270_groups t
  JOIN gk_0270_groups s
    ON s.namespace = t.namespace AND s.pod_name = t.pod_name AND s.seg = t.seg
   AND s.is_source = 1 AND s.grp = CASE WHEN t.grp = 0 THEN 1 ELSE t.grp END
 WHERE t.is_target = 1;

UPDATE combined_metrics
   SET owner_kind = s.owner_kind,
       owner_name = s.owner_name,
       cpu_request = COALESCE(s.cpu_request, 0),
       memory_request = COALESCE(s.memory_request, 0),
       metadata_source = CASE WHEN s.owner_kind IS NULL OR s.owner_name IS NULL
                              THEN 'repaired_partial' ELSE 'repaired' END,
       model_version = COALESCE(combined_metrics.model_version, '') || '+metadata-repaired'
  FROM gk_0270_repair r
  JOIN combined_metrics s ON s.rowid = r.source_id
 WHERE combined_metrics.rowid = r.target_id;

-- New row ids above every existing one: the workload usage rollup re-rolls these hours.
UPDATE combined_metrics
   SET rowid = rowid + (SELECT MAX(rowid) FROM combined_metrics)
 WHERE rowid IN (SELECT target_id FROM gk_0270_repair);

UPDATE sqlite_sequence
   SET seq = (SELECT MAX(rowid) FROM combined_metrics)
 WHERE name = 'combined_metrics';

DROP TABLE IF EXISTS gk_0270_repair;

DROP TABLE IF EXISTS gk_0270_groups;

DROP TABLE IF EXISTS gk_0270_rows;

DROP TABLE IF EXISTS gk_0270_pods;

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

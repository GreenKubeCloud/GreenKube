-- 0007: Fix duplicate active recommendations caused by NULL pod_name/namespace (SQLite).
--
-- SQLite treats NULL != NULL in unique indexes too. We deduplicate existing
-- rows and the application upsert already uses IS NULL checks for matching.

-- Remove duplicates keeping the most-recent row per (pod_name, namespace, type) / status='active'.
DELETE FROM recommendation_history
WHERE status = 'active'
  AND id NOT IN (
      SELECT id FROM (
          SELECT id,
                 ROW_NUMBER() OVER (
                     PARTITION BY COALESCE(pod_name, ''), COALESCE(namespace, ''), type
                     ORDER BY created_at DESC, id DESC
                 ) AS rn
          FROM recommendation_history
          WHERE status = 'active'
      ) ranked
      WHERE rn = 1
  );

-- 0007: Fix duplicate active recommendations caused by NULL pod_name/namespace.
--
-- PostgreSQL's unique index treats NULL != NULL, so ON CONFLICT never fires
-- for cluster-level recommendations (pod_name IS NULL, namespace IS NULL).
-- We replace the partial unique index with a functional one using COALESCE
-- so that (NULL, NULL, type) is treated as ('', '', type) for uniqueness.
-- The application upsert now uses IS NOT DISTINCT FROM instead of ON CONFLICT.

-- Remove duplicates: for each (pod_name, namespace, type) group where status='active',
-- keep only the most-recent row.
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

-- Drop the old partial unique index (NULL-unsafe).
DROP INDEX IF EXISTS idx_reco_active_key;

-- Rebuild with COALESCE so NULLs are treated as empty strings for uniqueness.
CREATE UNIQUE INDEX IF NOT EXISTS idx_reco_active_key
    ON recommendation_history (COALESCE(pod_name, ''), COALESCE(namespace, ''), type)
    WHERE status = 'active';

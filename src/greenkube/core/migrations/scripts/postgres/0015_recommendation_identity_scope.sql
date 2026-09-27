-- 0015: Align the active recommendation unique index with the application identity.
--
-- The application upserts recommendations on
-- (scope, namespace, pod_name, target_node, type), but the index created in
-- migration 0007 only covered (pod_name, namespace, type). Two active
-- node-scope recommendations of the same type therefore collided on
-- (NULL, NULL, type) and the second INSERT raised a UniqueViolationError.

-- Remove duplicates using the real application identity, keeping the most-recent row.
DELETE FROM recommendation_history
WHERE status = 'active'
  AND id NOT IN (
      SELECT id FROM (
          SELECT id,
                 ROW_NUMBER() OVER (
                     PARTITION BY
                         COALESCE(scope, 'pod'),
                         COALESCE(namespace, ''),
                         COALESCE(pod_name, ''),
                         COALESCE(target_node, ''),
                         type
                     ORDER BY created_at DESC, id DESC
                 ) AS rn
          FROM recommendation_history
          WHERE status = 'active'
      ) ranked
      WHERE rn = 1
  );

DROP INDEX IF EXISTS idx_reco_active_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reco_active_key
    ON recommendation_history (
        COALESCE(scope, 'pod'),
        COALESCE(namespace, ''),
        COALESCE(pod_name, ''),
        COALESCE(target_node, ''),
        type
    )
    WHERE status = 'active';

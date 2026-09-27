-- 0015: Deduplicate active recommendations using the real application identity (SQLite).
--
-- SQLite has no partial unique index for active recommendations. This migration
-- only removes rows that are duplicates under the identity used by the
-- application upsert: (scope, namespace, pod_name, target_node, type).

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

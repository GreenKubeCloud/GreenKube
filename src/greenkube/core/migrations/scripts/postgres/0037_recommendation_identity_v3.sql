-- 0037: include workload ownership in the active recommendation identity.

WITH identified AS (
    SELECT
        id,
        action_status,
        created_at,
        updated_at,
        CASE
            WHEN COALESCE(NULLIF(namespace, ''), '') = ''
                 AND COALESCE(NULLIF(pod_name, ''), '') = ''
                 AND COALESCE(NULLIF(container_name, ''), '') = ''
                 AND COALESCE(NULLIF(target_node, ''), '') = ''
                 AND COALESCE(NULLIF(owner_kind, ''), '') = ''
                 AND COALESCE(NULLIF(owner_name, ''), '') = ''
            THEN 'v3:legacy:' || CAST(id AS TEXT)
            ELSE 'v3:' ||
                CASE WHEN scope IS NULL OR scope = '' THEN 'pod' ELSE scope END || ':' ||
                COALESCE(NULLIF(namespace, ''), '') || ':' ||
                COALESCE(NULLIF(pod_name, ''), '') || ':' ||
                COALESCE(NULLIF(container_name, ''), '') || ':' ||
                COALESCE(NULLIF(target_node, ''), '') || ':' ||
                COALESCE(NULLIF(owner_kind, ''), '') || ':' ||
                COALESCE(NULLIF(owner_name, ''), '') || ':' ||
                COALESCE(type, '')
        END AS new_fingerprint
    FROM recommendation_history
    WHERE status = 'active'
),
ranked AS (
    SELECT
        id,
        action_status,
        updated_at,
        ROW_NUMBER() OVER (
            PARTITION BY new_fingerprint
            ORDER BY
                CASE
                    WHEN action_status = 'pr_open' THEN 0
                    WHEN action_status IN ('plan_ready', 'grouped') THEN 1
                    ELSE 2
                END,
                created_at DESC,
                id DESC
        ) AS duplicate_rank
    FROM identified
)
INSERT INTO automation_state_migration_audit (
    migration_version, entity_type, entity_id, recommendation_id,
    disposition, reason_code, reason, previous_status, resulting_status, previous_updated_at
)
SELECT
    37, 'recommendation', CAST(id AS TEXT), id,
    'quarantined', 'duplicate_active_identity',
    'Duplicate active recommendation identity after adding workload owner, stale duplicate retained for audit.',
    'active', 'stale', updated_at
FROM ranked
WHERE duplicate_rank > 1;

WITH identified AS (
    SELECT
        id,
        action_status,
        created_at,
        CASE
            WHEN COALESCE(NULLIF(namespace, ''), '') = ''
                 AND COALESCE(NULLIF(pod_name, ''), '') = ''
                 AND COALESCE(NULLIF(container_name, ''), '') = ''
                 AND COALESCE(NULLIF(target_node, ''), '') = ''
                 AND COALESCE(NULLIF(owner_kind, ''), '') = ''
                 AND COALESCE(NULLIF(owner_name, ''), '') = ''
            THEN 'v3:legacy:' || CAST(id AS TEXT)
            ELSE 'v3:' ||
                CASE WHEN scope IS NULL OR scope = '' THEN 'pod' ELSE scope END || ':' ||
                COALESCE(NULLIF(namespace, ''), '') || ':' ||
                COALESCE(NULLIF(pod_name, ''), '') || ':' ||
                COALESCE(NULLIF(container_name, ''), '') || ':' ||
                COALESCE(NULLIF(target_node, ''), '') || ':' ||
                COALESCE(NULLIF(owner_kind, ''), '') || ':' ||
                COALESCE(NULLIF(owner_name, ''), '') || ':' ||
                COALESCE(type, '')
        END AS new_fingerprint
    FROM recommendation_history
    WHERE status = 'active'
),
ranked AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY new_fingerprint
               ORDER BY
                   CASE
                       WHEN action_status = 'pr_open' THEN 0
                       WHEN action_status IN ('plan_ready', 'grouped') THEN 1
                       ELSE 2
                   END,
                   created_at DESC,
                   id DESC
           ) AS duplicate_rank
    FROM identified
)
UPDATE recommendation_history
SET status = 'stale',
    action_status = 'none',
    updated_at = NOW()
WHERE id IN (SELECT id FROM ranked WHERE duplicate_rank > 1);

UPDATE recommendation_history
SET identity_version = 3,
    fingerprint = CASE
        WHEN COALESCE(NULLIF(namespace, ''), '') = ''
             AND COALESCE(NULLIF(pod_name, ''), '') = ''
             AND COALESCE(NULLIF(container_name, ''), '') = ''
             AND COALESCE(NULLIF(target_node, ''), '') = ''
             AND COALESCE(NULLIF(owner_kind, ''), '') = ''
             AND COALESCE(NULLIF(owner_name, ''), '') = ''
        THEN 'v3:legacy:' || CAST(id AS TEXT)
        ELSE 'v3:' ||
            CASE WHEN scope IS NULL OR scope = '' THEN 'pod' ELSE scope END || ':' ||
            COALESCE(NULLIF(namespace, ''), '') || ':' ||
            COALESCE(NULLIF(pod_name, ''), '') || ':' ||
            COALESCE(NULLIF(container_name, ''), '') || ':' ||
            COALESCE(NULLIF(target_node, ''), '') || ':' ||
            COALESCE(NULLIF(owner_kind, ''), '') || ':' ||
            COALESCE(NULLIF(owner_name, ''), '') || ':' ||
            COALESCE(type, '')
    END
WHERE identity_version < 3 OR fingerprint IS NULL;

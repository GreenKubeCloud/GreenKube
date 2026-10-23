-- 0016: Stable recommendation identity v2 and container-aware identity (SQLite).
ALTER TABLE recommendation_history ADD COLUMN container_name TEXT;
ALTER TABLE recommendation_history ADD COLUMN identity_version INTEGER NOT NULL DEFAULT 2;
ALTER TABLE recommendation_history ADD COLUMN fingerprint TEXT;

UPDATE recommendation_history
SET fingerprint = 'v2:' || COALESCE(scope, 'pod') || ':' ||
    COALESCE(namespace, '') || ':' || COALESCE(pod_name, '') || ':' ||
    COALESCE(container_name, '') || ':' || COALESCE(target_node, '') || ':' || type
WHERE fingerprint IS NULL;

DELETE FROM recommendation_history
WHERE status = 'active' AND id NOT IN (
    SELECT id FROM (
        SELECT id, ROW_NUMBER() OVER (
            PARTITION BY fingerprint ORDER BY created_at DESC, id DESC
        ) AS rn
        FROM recommendation_history WHERE status = 'active'
    ) ranked WHERE rn = 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reco_active_fingerprint
    ON recommendation_history (fingerprint) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_reco_fingerprint ON recommendation_history (fingerprint);

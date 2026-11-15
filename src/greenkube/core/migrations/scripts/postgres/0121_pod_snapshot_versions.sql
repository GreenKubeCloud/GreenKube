-- 0121: Pod and node history fixes from the WP-01.1 review.
-- pod_snapshots: one row per observation interval instead of one row per spec hash, so a spec
-- rolled back A, B, A resolves to A after the rollback. A pod UID change also starts a new version.
-- pod_uid and cluster_uid are recorded. The unique key per spec hash comes from 0120 (same release).
-- node_snapshots_scd.last_seen: last time an unchanged node version was confirmed by the API,
-- so the max-age check of a removed node uses its last confirmation instead of valid_from.

ALTER TABLE pod_snapshots DROP CONSTRAINT IF EXISTS pod_snapshots_namespace_pod_name_spec_hash_key;

ALTER TABLE pod_snapshots
    ADD COLUMN IF NOT EXISTS cluster_uid TEXT,
    ADD COLUMN IF NOT EXISTS pod_uid TEXT;

CREATE INDEX IF NOT EXISTS idx_pod_snapshots_pod ON pod_snapshots (namespace, pod_name, first_seen);

ALTER TABLE node_snapshots_scd ADD COLUMN IF NOT EXISTS last_seen TIMESTAMPTZ;

UPDATE node_snapshots_scd scd
SET last_seen = (
    SELECT MAX(ns.timestamp) FROM node_snapshots ns
    WHERE ns.node_name = scd.node_name AND ns.timestamp >= scd.valid_from
)
WHERE scd.is_current = TRUE AND scd.last_seen IS NULL;

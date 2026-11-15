-- 0121: Pod and node history fixes from the WP-01.1 review.
-- pod_snapshots: one row per observation interval instead of one row per spec hash, so a spec
-- rolled back A, B, A resolves to A after the rollback. A pod UID change also starts a new version.
-- pod_uid and cluster_uid are recorded. The table comes from 0120 (same release), so it is rebuilt.
-- node_snapshots_scd.last_seen: last time an unchanged node version was confirmed by the API,
-- so the max-age check of a removed node uses its last confirmation instead of valid_from.

CREATE TABLE IF NOT EXISTS pod_snapshots_0121 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_uid TEXT,
    namespace TEXT NOT NULL,
    pod_name TEXT NOT NULL,
    pod_uid TEXT,
    spec_hash TEXT NOT NULL,
    owner_kind TEXT,
    owner_name TEXT,
    containers TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

INSERT INTO pod_snapshots_0121
    (namespace, pod_name, spec_hash, owner_kind, owner_name, containers, first_seen, last_seen)
SELECT namespace, pod_name, spec_hash, owner_kind, owner_name, containers, first_seen, last_seen
FROM pod_snapshots;

DROP TABLE pod_snapshots;

ALTER TABLE pod_snapshots_0121 RENAME TO pod_snapshots;

CREATE INDEX IF NOT EXISTS idx_pod_snapshots_last_seen ON pod_snapshots (last_seen);
CREATE INDEX IF NOT EXISTS idx_pod_snapshots_pod ON pod_snapshots (namespace, pod_name, first_seen);

ALTER TABLE node_snapshots_scd ADD COLUMN last_seen TEXT;

UPDATE node_snapshots_scd
SET last_seen = (
    SELECT MAX(ns.timestamp) FROM node_snapshots ns
    WHERE ns.node_name = node_snapshots_scd.node_name AND ns.timestamp >= node_snapshots_scd.valid_from
)
WHERE is_current = 1;

-- 0120: Provenance of node and pod metadata on raw metric rows (WP-01.1, COL-01, RECON-05).
-- node_source: snapshot, current_api or unknown. metadata_source: k8s_snapshot or missing.
-- Rows written before stay NULL (unknown provenance: built from the Kubernetes state at collection time).
-- pod_snapshots keeps one version per pod spec (requests, limits, owner, containers) with the
-- interval it was observed, so a later re-collection resolves deleted pods from it.

ALTER TABLE combined_metrics
    ADD COLUMN IF NOT EXISTS node_source TEXT,
    ADD COLUMN IF NOT EXISTS metadata_source TEXT;

ALTER TABLE container_metrics
    ADD COLUMN IF NOT EXISTS node_source TEXT,
    ADD COLUMN IF NOT EXISTS metadata_source TEXT;

CREATE TABLE IF NOT EXISTS pod_snapshots (
    id BIGSERIAL PRIMARY KEY,
    namespace TEXT NOT NULL,
    pod_name TEXT NOT NULL,
    spec_hash TEXT NOT NULL,
    owner_kind TEXT,
    owner_name TEXT,
    containers JSONB NOT NULL,
    first_seen TIMESTAMPTZ NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL,
    UNIQUE (namespace, pod_name, spec_hash)
);

CREATE INDEX IF NOT EXISTS idx_pod_snapshots_last_seen ON pod_snapshots (last_seen);

-- 0009: Persist node active/inactive status (PostgreSQL)

ALTER TABLE node_snapshots ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE node_snapshots_scd ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;

CREATE INDEX IF NOT EXISTS idx_node_scd_activity ON node_snapshots_scd(node_name, is_current, is_active);
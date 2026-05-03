-- 0009: Persist node active/inactive status (SQLite)

ALTER TABLE node_snapshots ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1;
ALTER TABLE node_snapshots_scd ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT 1;

CREATE INDEX IF NOT EXISTS idx_node_scd_activity ON node_snapshots_scd(node_name, is_current, is_active);
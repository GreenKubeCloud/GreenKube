-- 0184: Inputs greenkube recompute needs to re-derive node idle rows (NRG-12, NRG-15).
-- host_share is the node's share of a physical host shared with other nodes (1.0 alone).
-- node_cpu_used_cores is the node's root-cgroup CPU (NULL when it was not measured).
-- Rows written before keep host_share NULL: recompute leaves the node-steps of the current
-- model alone and re-derives older ones from the node snapshots.

ALTER TABLE node_idle_metrics ADD COLUMN IF NOT EXISTS host_share DOUBLE PRECISION;
ALTER TABLE node_idle_metrics ADD COLUMN IF NOT EXISTS node_cpu_used_cores DOUBLE PRECISION;

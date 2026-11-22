-- 0183: Host and provider ids in node snapshots (NRG-15).
-- host_id is status.nodeInfo.bootID and provider_id is spec.providerID: nodes with the same
-- boot id (or a kind:// or k3d:// provider id) are one physical host. Projections and
-- greenkube recompute read them to split the host baseline like the collection does.
-- Versions written before stay NULL. The next node save opens one new version per node,
-- and a reboot (new boot id) opens a new version.

ALTER TABLE node_snapshots_scd ADD COLUMN IF NOT EXISTS host_id TEXT;
ALTER TABLE node_snapshots_scd ADD COLUMN IF NOT EXISTS provider_id TEXT;

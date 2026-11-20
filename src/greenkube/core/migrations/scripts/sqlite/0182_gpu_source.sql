-- 0182: Provenance of GPU power (NRG-13).
-- gpu_source on raw rows: dcgm, gpu_tdp_estimate or unmeasured. NULL when no GPU was
-- detected on the node, and on rows written before (a GPU node without DCGM had no GPU power).

ALTER TABLE combined_metrics ADD COLUMN gpu_source TEXT;
ALTER TABLE container_metrics ADD COLUMN gpu_source TEXT;
ALTER TABLE node_idle_metrics ADD COLUMN gpu_source TEXT;

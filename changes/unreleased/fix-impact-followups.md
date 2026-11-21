### Added
- `GET /nodes` returns `gpu_source` for each node (from its latest idle row of the last hour), and the Footprint nodes page shows a "GPU power estimated" or "GPU power not measured" badge, with a notice counting such nodes, when no DCGM exporter sample reached GreenKube (NRG-13).

### Fixed
- `greenkube recompute` splits shared hosts (kind, k3d, multi-node minikube) for rows collected before migration 0183: when the node snapshot in force has no host or provider id, the ids of the node's current snapshot form the group, and the "Shared host" reason says the grouping came from the current snapshot. Rows an earlier recompute stamped with a host share of 1 for such nodes are repaired by `greenkube recompute --all`.
- The hourly rollups (`combined_metrics_hourly`, `container_metrics_hourly`, `node_idle_metrics_hourly`, `workload_usage_hourly`) of the 0.3.x upgrade hour, which mixes 5-minute rows with and without a CPU/RAM cost split, store NULL `cost_cpu` and `cost_ram` (and trusted components) instead of a partial sum, so projections and observed node prices treat it as an unsplit hour (F-03). Hours already compressed keep their stored values until they are re-aggregated.

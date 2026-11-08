### Added

- Stored metric rows are allocated by the Impact Model (roadmap B2): every node and collection step runs `ImpactModel.allocate`, so pod rows carry the reservation-aware energy, operational and embodied CO2e (idle power and embodied by reservation, dynamic power by usage) and record `model_version`. The unreserved remainder of each node is an explicit `__idle__` row (`scope: idle`) in the new `node_idle_metrics` table, and pod rows plus idle rows add up to the node totals (conservation tests). Idle rows never appear as pods in dashboards or analyzers. The collection job commits them after its leader-fence check (roadmap A14), like pod rows; their cost is modeled at list prices or custom rates and labeled so.
- `MemoryPowerModel` (roadmap B5): memory power of `POWER_MEMORY_W_PER_GIB` (Helm `impact.memoryWattsPerGiB`, default 0.392 W/GiB, the Cloud Carbon Footprint coefficient) per GiB of node memory, allocated by memory reservation. Memory request cuts now project the memory power they release (`co2e_breakdown_annual_g.memory_operational`). Impact Model version `1.1.0`.
- `GpuPowerModel` and DCGM collection (roadmap B5): GPU power from `DCGM_FI_DEV_POWER_USAGE`, or from the bundled board-power table `data/gpu_profiles.json` when DCGM reports utilization only. GPU power is charged to the pod DCGM attributes each GPU to (`gpu_power_watts`) and to `__idle__` otherwise; `gpu_usage_millicores` is now filled from `DCGM_FI_DEV_GPU_UTIL` (1000 = one fully used GPU). Container rows carry these GPU fields only for single-container pods.
- `RealizationContext` (roadmap B6): the realization factor is derived per node pool from the Cluster Autoscaler status ConfigMap, Karpenter NodePool consolidation policies and disruption budgets, `do-not-disrupt`/`safe-to-evict` annotations and blocking PodDisruptionBudgets (no autoscaler 0.0, Karpenter consolidation 0.8, Cluster Autoscaler 0.6, blockers ×0.5). The controller refreshes it from the collection cycle; projections record the factor, its source, `realization_reasons` and the pools used. An unreadable autoscaler input keeps the pool's previous observation (or the unobserved 1.0) instead of counting as "no autoscaler".
- `greenkube recompute --since <duration|timestamp> [--until] [--all] [--dry-run]` re-allocates raw rows within retention with the current Impact Model, rewriting pod, container and `__idle__` rows (idempotent: stored modeled costs are never read back as observed prices); hourly aggregates keep their original version (roadmap B8).
- Migration 0050: `model_version` and `gpu_power_watts` on `combined_metrics` and `container_metrics`, and the `node_idle_metrics` table (SQLite and PostgreSQL).

### Changed

- `IMPACT_REALIZATION_FACTOR` (Helm `impact.realizationFactor`) is now unset by default and acts as an override of the RealizationContext. Before the controller has observed the cluster, projections keep a factor of 1.0 with `realization_factor_source: realization_context:unobserved`.
- Pods without an observed OpenCost cost are charged their reservation at the node's list price or custom rate, labeled `cost_source: pricing_catalog` or `custom_rate` with an estimation reason, instead of `DEFAULT_COST` (BUG-17).
- `impact.types.CostSource` and `impact.types.PowerSource` are now the provenance enums of `greenkube.models.provenance` (one `CostSource` and one `PowerSource`; both import paths keep working).
- The energy estimator uses the Impact Model's `LinearCpuPowerModel`; the range pipeline's calculation version is `+range.3`.

### Fixed

- Cost and energy now use the same allocation boundary: energy no longer puts 100% of node power on pods by CPU usage while cost excluded idle capacity (BUG-19).
- Energy includes memory and GPU power, not CPU only (BUG-18).

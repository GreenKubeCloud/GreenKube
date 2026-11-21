# Impact Model v1 — methodology

**Model version:** `4.0.0` (`greenkube.impact.IMPACT_MODEL_VERSION`) · **Code:** `src/greenkube/impact/` · **Roadmap:** B1–B6, B8

The Impact Model is the single set of formulas GreenKube uses to say what a workload **is charged** (allocated cost and allocated CO₂e), what it **draws** (energy), and what a proposed change **releases**. Recommendation projections and the metric rows collection stores both come from it, so a projection and its later measurement are computed the same way.

This page is the reference for code review. When a formula below changes the numbers the model produces, the change must bump the model version: the major version when an existing formula changes, the minor version when a component is added and the existing ones keep their values.

| Version | Change |
|---|---|
| `1.0.0` | Reservation-aware allocation and projections (CPU power, embodied, cost). |
| `1.1.0` | Memory power (`MemoryPowerModel`) and GPU power (`GpuPowerModel`), allocated and projected. Stored rows carry the version that allocated them. |
| `2.0.0` | Projections are annualized over the target's [observed coverage](#annualization-and-observed-coverage) instead of the whole lookback, with a minimum coverage and a short-coverage note. Projected CO₂e is reported by [component](#co2e-components-and-component-matched-comparison) (operational energy, idle share, embodied) with the value a measurement is compared with. Allocation is unchanged. |
| `3.0.0` | Workload deletions take the pool's [realization factor](#realization-factor) for their reserved parts (IMP-05): per-asset costs (storage, LoadBalancer, network) and the usage-based CO₂e (dynamic CPU and GPU power) are saved whole, the rest at the factor; `co2e_expected_breakdown_annual_g` reports the expected CO₂e per component. `capacity_released` gains `storage_gib` and `load_balancers` (asset deletions). Allocation is unchanged: re-allocating stored rows (`greenkube recompute`) gives the same values. Recommendations are re-projected at the next scan; stored projections keep the version that made them. |
| `4.0.0` | A baseline that mixes hours with a CPU/RAM cost split and hours with a total only (hours re-aggregated from 0.3.x rows, F-03) prices each component over the hours that carry it and [apportions](#anchoring-to-the-observed-cost) the total of the other hours. Before, the split cost was spread over every hour of the window, so projected cost savings were up to several hundred times too low during the 14 days after an upgrade from 0.3.x. The per-node observed prices (tier 1) count the reserved quantities of the split hours only, for the same reason. `assumptions.unsplit_cost_hours_share` records the share of observed hours whose cost was apportioned. Allocation formulas are unchanged, but the version change marks every stored row as outdated: the next `greenkube recompute` (without `--all`) rewrites them with the same values and the new version. Recommendations are re-projected at the next scan; stored projections keep the version that made them. |

## Status of this version

| Part | Status |
|---|---|
| Projections for analyzers and connectors (`ImpactModel.project`) | **In use.** Every native analyzer with a savings figure, plus the VPA and Karpenter connectors, calls the model. The projection is stored as `impact` on each recommendation. |
| Reservation-aware allocation (`ImpactModel.allocate`) | **In use for stored metrics.** Collection allocates every node and step with the model: pod rows carry the allocated energy, CO₂e, embodied emissions and (without OpenCost) modeled cost, with `model_version`; the unreserved remainder of each node is an `__idle__` row ([Stored rows](#stored-rows-and-the-__idle__-workload)). |
| Cost fallback chain (observed → catalog → custom rates) | **In use for projections and stored rows.** Each sample is anchored to its own observed cost when the row has one. Rows without an observed cost are priced at list prices or custom rates and labeled with that tier. |
| Realization factor | **Per node pool** from the cluster's autoscalers, consolidation policies and disruption blockers ([RealizationContext](#realization-factor)). `IMPACT_REALIZATION_FACTOR` overrides it when set. |
| Recalculation (`greenkube recompute`) | Raw rows within retention can be re-allocated after a model change ([Recalculation](#recalculation-b8)). |
| `realize()` (node-hours actually removed, for the ledger) | Not part of v1. |

## Vocabulary

GreenKube never adds these together and always labels them (product principle 3).

| Term | Meaning |
|---|---|
| **Allocated** | The share of node cost and emissions a workload is charged under the allocation policy below. |
| **Projected** | The model's estimate of how much an allocated value changes if a proposal is applied. Every number on this page is projected unless stated otherwise. |
| **Expected (billable)** | Projected allocated change × realization factor: the part expected to become removed node-hours on the bill. |
| **Realized / measured** | What verification measures after the change. Not produced by projections. |

## Inputs

The model works on **samples**: one row per workload (or container), node and interval. A sample comes from a stored metric row.

| Field | Unit | Source |
|---|---|---|
| `hours` | h | `duration_seconds / 3600`, or the Prometheus step × `sample_count` when the duration is missing |
| `cpu_request`, `cpu_usage` | cores | `cpu_request / 1000`, `cpu_usage_millicores / 1000` |
| `mem_request`, `mem_usage` | GiB | bytes / 1024³ |
| `pue`, `grid_intensity` | –, gCO₂e/kWh | the row values, falling back to the node profile |
| observed costs | currency | `total_cost`, and `cost_cpu` / `cost_ram` / `cost_storage` / `cost_network` / `cost_lb` when present |

A **node profile** holds what the model knows about a node:

- CPU capacity (cores) and memory capacity (GiB);
- idle power `P_min` and full-load power `P_max` (W);
- memory power per GiB `w_mem` (W/GiB, `POWER_MEMORY_W_PER_GIB`, default 0.392);
- the power of GPUs DCGM attributes to no pod, `P_gpu_idle` (W);
- embodied emissions per hour `M_h` (gCO₂e/h);
- unit prices `p_cpu` (per core-hour) and `p_ram` (per GiB-hour);
- PUE and grid intensity.

### State at sample time (WP-01.1)

A window is collected after it ended, when some of its pods and nodes may already be gone from the Kubernetes API. The collector therefore reads the Kubernetes facts of each sample as they were at the sample time:

- **Node attributes** (zone and Electricity Maps zone, provider and PUE, instance type, capacity, power and embodied profiles) come from the `node_snapshots_scd` version valid at the sample time, else the earliest later version, else the node as the API lists it now. A version is ignored only when the sample is more than `NODE_DATA_MAX_AGE_DAYS` after the end of the version or, for the current version, after the last time the API confirmed it unchanged (`last_seen`, migration 0121): a node that never changed keeps its snapshot however old its `valid_from` is. Each row stores where they came from in `node_source`: `snapshot`, `current_api` or `unknown` (configured defaults, also flagged by the existing zone and power reasons).
- **Pod metadata** (owner, requests, limits and container list) comes from the `pod_snapshots` history, which every collection extends with the live pod list. A version is one observation interval: an observation with the same spec and pod UID as the pod's latest version extends it, anything else starts a new version, so a spec rolled back (A, B, A) resolves to A after the rollback and a pod recreated under the same name (new UID) is a new version. Versions last observed more than `NODE_DATA_MAX_AGE_DAYS` ago are pruned. The version first observed at or before the sample time is used, else the earliest one, else the live pod list, else (for a pod never observed live) the owner, requests, limits and containers kube-state-metrics reports at that step (`kube_pod_owner`, `kube_pod_container_resource_requests`, `kube_pod_container_resource_limits`; rows say `metadata_source = kube_state_metrics`); pods of the window that the range read does not cover (for example deleted during a long controller downtime) are looked up by name without lower bound. Rows store `metadata_source = k8s_snapshot`. When the sample lies in an unobserved gap after the version and the next version is another incarnation of the pod (a different or unknown UID), or precedes the pod's first observation by more than two steps (first-install backfill, downtime), kube-state-metrics is used for that step when it has the pod; otherwise the version is kept and, unless the owner gives its pods generated names (Deployment, ReplicaSet, DaemonSet, Job, CronJob, Rollout), the row is flagged with `pod_metadata_uncertain`. For pods of generated-name owners sampled before their first observation without kube-state-metrics, the first observed spec is used unflagged: a recreated pod would have another name, but an in-place resize (Kubernetes 1.33+) made before that first observation is not seen. kube-state-metrics queries run in chunks of at most 50 pod names and about 4 KB of names; a failed chunk does not discard the others, and the rows of its pods carry the reason `kube_state_metrics_unavailable`. Native sidecars without requests get a container entry with request 0. kube-state-metrics queries are restricted to the pods concerned (by name, 50 per query) and cover containers without requests and native sidecars (init containers with `restartPolicy: Always`).
- **Workload owner.** The owner of a live pod is the end of its controller chain (COL-16): a ReplicaSet resolves to its controller ownerReference (Deployment, Argo Rollout, or none: the ReplicaSet itself), and a Job created by a CronJob resolves to the CronJob, so every run of a CronJob is one workload. ReplicaSets and Jobs are listed once per collection; when the ReplicaSet list fails (missing RBAC), the name rule `<deployment>-<pod-template-hash>` is kept and a warning is logged. Owners read from kube-state-metrics (`kube_pod_owner`, pods never observed live) follow the same chain through `kube_replicaset_owner` and `kube_job_owner`; when those queries fail, the name rule is kept for ReplicaSets and Jobs stay owners, with a warning. Rows stored before keep their owner.
- **Re-collection.** A pod known neither from the history nor from the live list takes the owner, requests and containers of the row already stored for it at that step (`metadata_source = stored_row`; rows without provenance, written before `+range.4`, are not reused; requests of the pod row that no container row covers, such as init containers, are kept on the pod row), and the allocation of its node and step is re-run with them, so energy, CO2e, cost and flags stay consistent with the requests. A pod known from none of them gets `metadata_source = missing`, the estimation reason `pod_metadata_missing`, no owner and requests 0; such a row never replaces a stored row that had metadata: the whole stored row and its container rows are kept. When the collection job writes a window, the container rows of each pod row are rewritten in the same transaction (stale ones deleted), so pod joules equal the sum of their container joules. A pod row for which the pass builds no container row (no container usage, for example a missing container series) is not rewritten when container rows are stored for it: the stored pod row and its container rows are kept together, so their joules still agree.
- **Kubernetes list failures.** A failed pod or node list fails the window (it is retried) unless the history covers it. After `COLLECTION_K8S_LIST_MAX_FAILURES` consecutive failed attempts for the same window, the window is stored in degraded mode instead of becoming a gap: rows without history carry the reason `k8s_list_failed` (and `metadata_source = missing` or `node_source = unknown`), counted in `greenkube_collection_degraded_windows_total{list}`.

Rows written before calculation version `+range.4` have `node_source` and `metadata_source` NULL: they were built from the state at collection time and are not rewritten. Migration 0270 repairs the metadata gaps of stored rows: a row with neither owner nor requests (`metadata_source` NULL or `missing`) takes the owner and the requests of the last earlier row of the same namespace and pod that has metadata (the last known values), else of the first later one, within one continuous run of rows of that pod: a gap over three steps, or a new `pod_snapshots` version (spec change or another incarnation of the pod name), ends the run, so values never cross incarnations. A row that has an owner or a request is never rewritten (BestEffort pods have requests 0). The row then says `metadata_source = repaired` (`repaired_partial` when the source had requests but no owner), its `model_version` gets the suffix `+metadata-repaired`, so `greenkube recompute` re-runs the allocation of its node and step, and it gets a new row id, so the workload usage rollup re-rolls its hour with the repaired owner. Pods that never had metadata keep owner NULL and requests 0. Container rows with owner NULL take their pod's owner when all its pod rows agree on it. The migration is linear in the table size. `+range.4` rows are not rewritten either; a later re-collection of their window applies the `+range.5` rules above.

The **allocatable** capacity `A` is the denominator of reservation shares. Until kubelet allocatable is collected, `A` equals the node capacity, which matches how OpenCost computes idle cost.

### Power profile

Collection, `greenkube recompute` and projections select the power profile with one function, `greenkube.energy.node_power.resolve_node_power_profile` (NRG-05), so a projection uses the watts of the rows its measurement later compares it with:

1. **`instance_profile`**: the instance type is in `data/instance_power_profiles.csv`, which holds Cloud Carbon Footprint per-vCPU coefficients times vCPUs.
2. **`node_capacity_derived`**: the instance type is unknown but the node CPU capacity is known (or a `cpu-N` type). The model uses the per-vCPU coefficients of the node's provider from `data/provider_power_estimates.csv` (for example 0.74 / 3.5 W per vCPU on AWS), or their cross-provider average (0.726 / 3.564 W) when the provider is unknown, times the capacity. See `docs/power_estimation_methodology.md`.
3. **`default_profile`**: nothing is known about the node. The model uses `DEFAULT_INSTANCE_MIN_WATTS` / `DEFAULT_INSTANCE_MAX_WATTS` for `DEFAULT_INSTANCE_VCORES`, and the projection is flagged with `unknown_node_profile`.

Impact Model 4.0.0 aligned the projector on this resolver; before, it used `DEFAULT_INSTANCE_MIN_WATTS / DEFAULT_INSTANCE_VCORES` (1 W idle, 10 W max per core) for every node without a bundled profile.

### CPU used outside pods (NRG-12)

The node utilisation is measured at node level when the kubelet exposes its root cgroup (`container_cpu_usage_seconds_total{id="/"}`, kubelet, container runtime, kernel and host processes included): `utilisation = max(root cgroup cores, Σ pod cores) / capacity`. Pods still receive `pod cores / capacity` of the dynamic range, and the rest, `(node − Σ pods) / capacity`, is the system share charged to the node's `__idle__` row, so pod and idle rows add up to the node power at its real utilisation. Without the root cgroup series the utilisation is the sum of the pods, as before. Node-exporter fallback and a stored `node_utilization_source` are not implemented yet.

### Nodes sharing one host (NRG-15)

kind, k3d and multi-node minikube nodes are containers of one machine and each report the host capacity. Nodes with the same `bootID` (or a `kind://`, `k3d://` provider ID) form a group of N: each node gets `idle_watts / N`, `memory_watts_per_gib / N` and `embodied / N`, while the dynamic range `max − idle` is not scaled because each node's utilisation is already a fraction of the host capacity. Pod and idle rows of such nodes carry a "Shared host" estimation reason. The grouping uses the live node list of the collection run. Projections apply the same split (`greenkube.energy.shared_host.scale_to_host_share`) to the node snapshots, which store `host_id` and `provider_id` since migration 0183. An operator embodied value (`greenkube.io/embodied-kg`, `EMBODIED_KG_BY_INSTANCE_TYPE`) on such nodes is the whole host's value: each of the N nodes gets 1/N, so annotate every node of the host with the host figure.

`greenkube recompute` re-derives a node-step from what its idle row recorded (migration 0184): `host_share` and the root-cgroup CPU `node_cpu_used_cores` (NRG-12), plus its `gpu_source` and the stored GPU and shared-host reasons (NRG-13). Node-steps of the current model whose idle row predates 0184 are left as stored (`groups_skipped_unrecorded` in the report). Older rows take the shared host from the node snapshots and the pods' CPU sum. A snapshot stored before migration 0183 has no host or provider id: recompute then uses the ids of the same node's current snapshot, and the "Shared host" reason ends with "grouping from the current node snapshot". A recorded `host_share` of 1 next to a node's own snapshot without ids was written by an earlier recompute that could not group the node, so the next recompute that reaches these rows replaces it with the group's share (`--all` for rows of the current model, which a default recompute skips). A group member whose own snapshot has ids keeps its recorded share. A re-allocation that drops an estimation reason recomputes `is_estimated` from the remaining reasons. Opt out with `config.power.sharedHostDetection: false`.

### Memory and GPU power (B5)

- **`MemoryPowerModel`**: `P_mem = w_mem × memory GiB`, with the Cloud Carbon Footprint coefficient 0.392 W/GiB by default. On an m5.large (8 GiB) fully reserved, memory adds about 3.1 W.
- **`GpuPowerModel`**: the board power DCGM measures (`DCGM_FI_DEV_POWER_USAGE`, averaged over the step). When only the GPU model and utilization are known, `P = idle + util × (TDP − idle)` with the TDP of `data/gpu_profiles.json` and idle at 15% of TDP. Each sample carries the GPU power DCGM attributes to its pod (`gpu_power_watts`); unattributed GPUs are node-level (`P_gpu_idle`).
- **GPU nodes without DCGM (NRG-13)**: the node collector reads the GPUs of the node capacity (`nvidia.com/gpu`, `amd.com/gpu`, `gpu.intel.com/*`) and the `nvidia.com/gpu.product` label. At a step without DCGM samples for such a node, each GPU a pod requests (container requests, or limits, of `nvidia.com/gpu`, `amd.com/gpu`, `gpu.intel.com/*`) is charged to that pod (`gpu_power_watts`) at `idle + util × (TDP − idle)` with the assumed utilisation `GPU_ASSUMED_UTILIZATION_PCT` (50 % by default); requests beyond the node's GPU count are capped. The GPUs no pod requests are counted at the idle floor of their TDP profile (a lower bound) as unattributed node power (`P_gpu_idle`, the `__idle__` row). Its rows carry the reason "GPU power estimated from TDP". When the model has no profile, the GPU power is not counted, rows carry "GPU power not measured" and `greenkube_gpu_nodes_unmeasured` counts the node. The detection uses the live node list of the collection run; GPU requests of past windows come from the pod snapshots (stored since this change) and are zero for older snapshots and pods known only from kube-state-metrics. Once the collector has seen DCGM series, a DCGM query error fails the window (it is retried) instead of storing it without GPU power. Pod, container and idle rows record `gpu_source` (migration 0182): `dcgm` when DCGM has samples for the node at that step, `gpu_tdp_estimate` for the TDP estimate, `unmeasured` for an unknown model, NULL when no GPU is detected on the node (and on rows written before).

### Embodied emissions

`M_h = GWP × 1000 / (lifespan_years × 8760)` gCO₂e per node-hour.

- `GWP` is resolved per node by `greenkube.energy.embodied.resolve_node_embodied`, the same function for collection, `greenkube recompute` and projections, and recorded on every pod, container and idle row as `embodied_source`:
  1. `configured`: the node annotation `greenkube.io/embodied-kg` (whole-node kg CO₂e, for on-prem hardware), then `EMBODIED_KG_BY_INSTANCE_TYPE` (Helm `config.boavizta.embodiedKgByInstanceType`);
  2. `boavizta`: the lifetime embedded GWP of the node's cloud instance type from the Boavizta API v1 (`impacts.gwp.embedded.value`, requested without `duration`), cached in `instance_carbon_profiles`;
  3. `fallback_per_vcpu`: `DEFAULT_EMBODIED_KG_PER_VCPU` (12 kg, the Boavizta m5 family ratio) × cores + `DEFAULT_EMBODIED_KG_PER_GIB` (0) × GiB;
  4. `fallback_default`: `DEFAULT_EMBODIED_EMISSIONS_KG` (100 kg) when the capacity is unknown.

  Rows of the two fallbacks get `is_estimated` and an estimation reason starting with "Embodied emissions estimated". A failed Boavizta lookup is stored as a negative cache entry (`source = 'fallback'`, `error`) and retried at most every 24 hours. Node snapshots carry only the configured value (NRG-07).
- The lifespan is `DEFAULT_HARDWARE_LIFESPAN_YEARS` (4 years).
- Impact Model 4.0.0 introduced this resolution; rows written before kept the flat 100 kg per node (`embodied_source` NULL). `greenkube recompute --since <raw retention>` re-allocates the raw rows; hourly aggregates keep the old values.

## Allocation policy: `ReservationAwareAllocation`

For one node and a window of `H` hours, with samples `i` on the node, the reservations are expressed in core-hours and GiB-hours:

```text
r_i(cpu) = max(cpu_request_i, cpu_usage_i) × hours_i
r_i(mem) = max(mem_request_i, mem_usage_i) × hours_i

share_i(res) = r_i(res) / max(A(res) × H, Σ_j r_j(res))      # reservation share
idle(res)    = 1 − Σ_i share_i(res)                           # goes to __idle__

util     = min(Σ_i cpu_usage_i × hours_i / (cores × H), 1)
use_i    = cpu_usage_i × hours_i / Σ_j cpu_usage_j × hours_j   # usage share
```

Node totals and how they are split:

| Quantity | Node total | Workload `i` | `__idle__` |
|---|---|---|---|
| CPU cost | `cores × p_cpu × H` | `share_i(cpu) × total` | `idle(cpu) × total` |
| RAM cost | `GiB × p_ram × H` | `share_i(mem) × total` | `idle(mem) × total` |
| Idle energy | `P_min × H / 1000` kWh | `share_i(cpu) × total` | `idle(cpu) × total` |
| Dynamic energy | `util × (P_max − P_min) × H / 1000` kWh | `use_i × total` | 0 |
| Memory energy | `w_mem × max(A(mem) × H, Σ_j r_j(mem)) / 1000` kWh | `w_mem × r_i(mem) / 1000` | `idle(mem) × total` |
| GPU energy | `(Σ_i P_gpu_i × hours_i + P_gpu_idle × H) / 1000` kWh | `P_gpu_i × hours_i / 1000` | `P_gpu_idle × H / 1000` |
| Operational CO₂e | energy × PUE × intensity | same split as its energy | same split as its energy |
| Embodied CO₂e | `M_h × H` | `share_i(cpu) × total` | `idle(cpu) × total` |

Properties:

- **Conservation.** The sum over workloads plus `__idle__` equals the node totals for cost, energy, operational and embodied CO₂e. A randomized test checks this.
- **One boundary for cost and carbon.** Idle power and embodied emissions follow reservations, like cost. A request cut therefore lowers a workload's allocated CO₂e the same way it lowers its allocated cost. The previous pipeline put 100% of node power on pods by usage share, while cost excluded idle (BUG-19).
- **Over-commit.** When reservations exceed capacity (usage above requests), shares are normalized by the total reservation. Nothing is left for `__idle__`.
- **Missing usage.** A sample without usage reserves its request only and receives no dynamic energy.

### Worked example (golden fixture, node `a`)

The node has 4 cores and 16 GiB, `P_min` = 40 W, `P_max` = 120 W, `M_h` = 10 g/h, `p_cpu` = 0.04, `p_ram` = 0.005, PUE 1.0, intensity 100 g/kWh, over one hour.

| Pod | CPU req / use | Mem req / use | Cost | Energy (kWh) | Op. CO₂e (g) | Embodied (g) |
|---|---|---|---|---|---|---|
| a1 | 1 / 0.5 | 4 / 2 | 0.060 | 0.020 | 2.0 | 2.5 |
| a2 | 0.5 / 1.0 | 2 / 3 | 0.055 | 0.030 | 3.0 | 2.5 |
| a3 | 0 / 0.5 | 0 / 1 | 0.025 | 0.015 | 1.5 | 1.25 |
| a4 | 1 / 0 | 4 / 0 | 0.060 | 0.010 | 1.0 | 2.5 |
| `__idle__` | | | 0.040 | 0.005 | 0.5 | 1.25 |
| **Node** | | | **0.240** | **0.080** | **8.0** | **10.0** |

The full fixture has three nodes and ten pods, including an over-committed node and a pod present for half the window. It is in `tests/impact/test_allocation.py` (memory power is 0 in that fixture; `tests/impact/test_power.py` covers memory and GPU power, with the same conservation test).

### Stored rows and the `__idle__` workload

Collection (`HistoricalRangeProcessor`) runs the allocation for every node and step:

- pod rows store the allocated `joules` (all four energy components), `co2e_grams` (operational, with the row's PUE and grid intensity), `embodied_co2e_grams`, `gpu_power_watts` and `gpu_usage_millicores`, and `model_version`;
- a pod with an observed OpenCost or Kubecost cost keeps it (`cost_source: opencost`, or `opencost_derived` when OpenCost's figure is below its precision, see [OpenCost precision](#opencost-precision-sub-precision-costs)); a pod without one is charged its reservation at the node's list price or custom rate, with `cost_source: pricing_catalog` or `custom_rate` and an estimation reason. A price derived from other pods' OpenCost costs is never used for such a row, so it can never pass for an observed cost;
- OpenCost is queried once per collected window (`accumulate=true`). In a window of several steps (catch-up after downtime, first-install backfill), each row receives the share of its pod's window allocation that matches the pod's running seconds inside the row's step, from the allocation's `start` and `end`: a pod that ran 3 of its 7 minutes in a step gets 3/7 of its cost there. The shares of a pod sum to 1, so the stored cost of a window equals OpenCost's allocation whatever the chunking. The share of a running step without a row is not stored and shows in the cost reconciliation of the step record (COL-10). Rows written before `+range.7` used `step / running time` per row, which over-costed pods that covered steps only partly;
- when OpenCost is configured or was discovered but cannot price a window (timeout, HTTP 401 or 503, invalid JSON), or answers without any allocation after it priced earlier windows (lag, gap in its own data), the window's rows keep the modeled cost with `cost_source: opencost_pending` and an estimation reason. `opencost_pending` is never trusted. The step records in `collection_windows` carry `cost_status: opencost_pending`, the watermark advances, and the gauge `greenkube_collection_cost_pending_steps` counts them. Once OpenCost answers again, each scheduled run re-collects up to one chunk of pending steps inside `COLLECTION_MAX_BACKFILL` (least recently attempted first) and replaces their rows with OpenCost's cost, which clears the status. A re-pricing pass that would lose stored pods is not written, and the steps stay pending. Steps that leave the horizon while still pending keep their untrusted rows. Once OpenCost has priced a window of the cluster (recorded in the `opencost_cost` watermark row, kept beyond the horizon), a pod whose allocation carries no usable cost (see [OpenCost precision](#opencost-precision-sub-precision-costs)) and an OpenCost whose URL can no longer be resolved also give `opencost_pending` rows and pending steps. Steps that leave the horizon while pending get `cost_status: opencost_pending_expired`, a warning and the counter `greenkube_collection_cost_pending_expired_steps_total`. A cluster without OpenCost (no URL configured, nothing discovered, never priced) gets the modeled tiers above and nothing is pending. A re-collection while OpenCost is down never replaces a stored OpenCost cost (cost source rank, STO-01);
- the node's unreserved share is written to `node_idle_metrics` (one row per node and step, `scope: idle`): idle and memory power, unattributed GPU power, embodied emissions and cost of the unreserved capacity (always priced at list prices or custom rates and labeled with that tier: OpenCost idle cost is not queried, so the idle cost is a model output even on clusters with OpenCost). The collection job commits idle rows together with the pod rows, after its leader-fence check, and does not advance its watermark when the idle write fails;
- container rows are split from their pod row and inherit its `model_version`. Energy and CO₂e follow each container's CPU usage share. Cost follows the reservation that OpenCost and the model charge: the pod's CPU cost by each container's `max(cpu_request, cpu_usage)`, its RAM cost by `max(memory_request, memory_usage)`, other cost components (storage, network, load balancer) by the CPU reservation share; the split of a resource is equal when neither request nor usage is known for any container, or when some container has no request and an unknown usage (its reservation is unknown, not zero) (COST-02). `greenkube recompute` applies the same rule. Before `+range.8` cost was split by usage, which under-costed a container with a large request and little usage (the target of rightsizing) and distorted the unit prices of its projections; container rows inside raw retention are re-split by `greenkube recompute --all` for modeled pods, and by re-collecting the window for OpenCost-priced pods. Hourly container aggregates and `workload_usage_hourly` keep the old split. DCGM attributes GPUs to pods, so only a single-container pod passes `gpu_power_watts` and `gpu_usage_millicores` to its container row; with several containers these fields stay empty rather than repeating the pod's GPU on each container.

Pod rows plus the idle row equal the node totals for energy, operational and embodied CO₂e. Cost conserves too when no pod carries an observed cost; with OpenCost, pod rows keep OpenCost's allocation. Only rows whose cost is really observed (`opencost`, `kubecost`) feed the observed price tier, so a recompute never reads a stored modeled cost back as an observed price and is idempotent. Idle rows live in their own table, so pod-level dashboards, analyzers and recommendations never see a synthetic `__idle__` pod. The compressor aggregates them into `node_idle_metrics_hourly` behind their own compression watermark and prunes them with the raw and hourly retentions like pod rows, and the carbon-intensity recompute refreshes them with the pod rows of the same step, so node totals stay reconstructible after the raw retention. `GET /metrics/summary` and `/metrics/timeseries` return the pod sums as allocated totals and, cluster-wide, the idle share (`idle_*`) and the cluster totals (`cluster_total_*` = allocated + idle); so do `/report/summary`, the dashboard summary cache, `/report/export?include_idle=true` (labelled `idle` and `cluster_total` rows), the Prometheus gauges `greenkube_cluster_idle_*` and `greenkube_cluster_with_idle_*`, and the Footprint page. They are unknown (null, or an absent Prometheus series), never zero, when the idle history does not cover the window. Samples whose node cannot be resolved are allocated against the default profile without an idle row.

### OpenCost precision: sub-precision costs

OpenCost writes allocation costs with 5 decimals. Collection queries one step at a time (for example 1 minute), so a small pod's cost per step is often below 5e-6 and reads `0`; a pod costing 7e-6 reads `1e-05` (+40%). A zero cost for a pod that ran with requests is otherwise treated as a missing price (`cost_source: none`), so every small pod would lose its trusted samples. Rounding also biases deltas: a pod rightsized from 10m to 5m CPU would read `1e-05` → `0`, a 100% saving.

OpenCost prices CPU and RAM as `cpuCoreHours × node CPU price` and `ramByteHours × node RAM price`. When an allocation's total is below **0.001** (where rounding exceeds 0.5% of the figure, a quarter of the verifier's 2% noise floor), the collector rebuilds its cost the same way:

1. **Node unit prices** come from OpenCost itself: one `aggregate=node` query over the 24 hours before the window (`accumulate=true`, re-read once per hour of collection time or sooner when a node has no price yet, and only when some allocation is below the floor). A node's price is `cpuCost / cpuCoreHours` and `ramCost / ramByteHours`, kept only when that node cost is at least 0.001 (precise to 0.5%). A node with no cost gets no price.
2. **Resource-hours** of the pod: `ramByteHours` (bytes, precise), and `max(cpuCoreRequestAverage, cpuCoreUsageAverage) × minutes / 60` clamped to the ±5e-6 interval the rounded `cpuCoreHours` allows.
3. **Agreement check**: the rebuilt CPU and RAM costs must each equal OpenCost's `cpuCost` and `ramCost` within ±5e-6 (rounding) + 2%, and the rebuilt total (with adjustments, storage, network, LoadBalancer and GPU as reported) must equal `totalCost` within ±1.5e-5 + 2%.

When all three hold, the row stores the rebuilt costs with `cost_source: opencost_derived`, which measurement trusts like `opencost`. Every allocation below the floor goes through the same rule, zero or not, so before and after a change are measured with the same precision and rounding neither creates nor erases a saving.

Rollups (the hourly tables written by the metrics compressor, `workload_usage_hourly` and the baseline rows read from it) label an aggregate of rows that are all `opencost` or `opencost_derived` as `opencost_derived`, which stays trusted. A pod near the floor, or a missed node price query, can mix both labels within one hour, and both are OpenCost's own allocation. The aggregate takes the derived label, not `opencost`, so provenance never hides that part of its cost was rebuilt. Any other mix (with `none`, `custom_rate`, `pricing_catalog`, `kubecost`, `mixed`, or rows with no source) is still `mixed` and untrusted.

The rule fails closed: a NaN, infinite, missing or negative OpenCost cost is never rebuilt; a zero that rounding cannot explain (a priced pod of 2 cores reported at 0, a node without an OpenCost price, an allocation without a node, a failed node price query) stays `cost_source: none` with the default placeholder; and an allocation at or above the floor keeps OpenCost's figures unchanged. A non-zero figure below the floor that cannot be rebuilt is not kept either: its rounding error is unbounded relative to its value, so it gets `cost_source: none` and the counter `greenkube_opencost_unrebuilt_allocations_total` counts it (COST-05). An allocation with no running time (`minutes = 0`, or a window whose start equals its end, which OpenCost returns after a gap in its own data or in the first window of a new pod) carries no cost whatever its requests, and a zero for a pod with a request or a usage average is a missing price: both get `cost_source: none`, and the row then receives the modeled cost with its own label. Only a zero for a pod that ran without any request or usage keeps `opencost`. At read time `is_cost_trusted` also refuses a zero total on any row with a request or with CPU or memory usage. Rows stored before this change keep their label; they are refused at read time by the same rule. A rebuilt cost is not "a price derived from other pods": it is only used for a pod OpenCost itself costed, and only when OpenCost's own figures agree with it.

## Unit prices: the cost fallback chain

Prices are resolved at two levels:

1. **Per node**, the unit prices `p_cpu(node)` and `p_ram(node)` come from the first available tier (1 to 3) of the table below.
2. **Per sample**, a projection then anchors the price to the sample's own observed cost when the row has one (tier 0, see [Anchoring to the observed cost](#anchoring-to-the-observed-cost)).

The tier is recorded with every projection (`assumptions.unit_prices[].source` and `.detail`).

| Tier | `source` | How the price is obtained | Confidence |
|---|---|---|---|
| 0. Observed, per sample | `opencost` / `opencost_derived` / `kubecost` (the row's `cost_source`) | The sample's observed cost divided by its reserved quantity (below). `detail` is `observed_components`, `observed_total_apportioned`, or `observed_components_partly_apportioned`. | 0.9, or 0.8 when apportioned from a total |
| 1. Observed, per node | `opencost` / `kubecost` | `Σ cost_cpu / Σ reserved core-hours` and `Σ cost_ram / Σ reserved GiB-hours` over the node's metric rows, counting only the hours that carry the split (F-03). Both resources must be observed with positive values. | 0.9 |
| 2. Pricing catalog | `pricing_catalog` | On-demand list price of the node's instance type in its region, split between CPU and RAM with the OpenCost ratio (below). | 0.75 |
| 3a. Custom rates | `custom_rate` (`configured`) | `IMPACT_PRICING_CPU_CORE_HOUR` and `IMPACT_PRICING_RAM_GIB_HOUR`. | 0.7 |
| 3b. Default rates | `custom_rate` (`opencost_default`) | OpenCost default custom pricing: 0.031611 per vCPU-hour and 0.004237 per GiB-hour (USD), described by OpenCost as "based on GCP us-central1". Source: [`configs/default.json`](https://github.com/opencost/opencost/blob/cee1642a5f8d7fcd280cd12f39f350cda2d99c82/configs/default.json), bundled as `data/pricing/opencost_default.json`. | 0.5 |

**Splitting a node price** follows OpenCost's algorithm for list prices that have no CPU/RAM breakdown (`pkg/costmodel/costmodel.go`):

```text
ratio     = default CPU price / default RAM price     # 0.031611 / 0.004237 ≈ 7.46
ram_price = node_price / (vcpus × ratio + ram_gib)
cpu_price = ram_price × ratio
```

The node price is preserved exactly: `vcpus × cpu_price + ram_gib × ram_price = node_price`.

**Catalog files** (`data/pricing/<provider>.json`) are generated by `scripts/generate_pricing_catalog.py` from public price lists. GreenKube never scrapes prices at runtime.

| Provider | Source | Coverage |
|---|---|---|
| AWS | Price List Bulk API, regional EC2 offer files (on-demand, shared tenancy, Linux) | Regions mapped to an Electricity Maps zone, except the China regions (not in the public bulk API); instance types with a power profile |
| Azure | Retail Prices API (`Virtual Machines`, consumption, Linux, no Spot or Low Priority) | Same. The API has no VM sizes, so node capacity comes from the node snapshot. |
| OVHcloud | Public order catalog, hourly `consumption` plans | All flavors. The price is the same in every standard region and is stored under the `"*"` region. Prices are in EUR. |
| Scaleway | Public product catalog, instance products | All instance types, keyed by region (`fr-par`, `nl-ams`, …). Prices are in EUR. |
| GCP | Not generated: the Cloud Billing Catalog API requires an API key, and the former key-less pricing-calculator list (`cloudpricingcalculator.appspot.com/static/data/pricelist.json`) is no longer published | Falls back to the default rates, which are GCP `us-central1` prices |

**Currency.** GreenKube never converts currencies. A catalog is used only when its currency equals `IMPACT_PRICING_CURRENCY`: the OVHcloud and Scaleway catalogs (EUR) price nodes only when the currency is set to `EUR`, otherwise those nodes fall through to the custom rates. Observed costs are labeled with `IMPACT_PRICING_CURRENCY`, which should therefore match the currency OpenCost reports. All the prices of one cluster share one currency, so the totals summed by the API are consistent. A projection that still mixes currencies (only possible with hand-built inputs) reports `currency: "mixed"`.

### Anchoring to the observed cost

Node unit prices are an estimate. When a metric row carries the cost OpenCost allocated to the container, that observed cost is what the container is charged, so a projection prices the sample from it. For a sample `s`, with reserved quantities `q_cpu = max(cpu_request, cpu_usage) × hours` and `q_mem = max(mem_request, mem_usage) × hours`:

```text
CPU and RAM costs observed:   p_cpu(s) = cost_cpu_s / q_cpu
                              p_ram(s) = cost_ram_s / q_mem
Only the total observed:      w_cpu = q_cpu × p_cpu(node)        w_mem = q_mem × p_ram(node)
                              p_cpu(s) = total_s × w_cpu / (w_cpu + w_mem) / q_cpu
                              p_ram(s) = total_s × w_mem / (w_cpu + w_mem) / q_mem
No observed cost:             p_cpu(s) = p_cpu(node)             p_ram(s) = p_ram(node)
```

- The second case is OpenCost's apportioning applied to the container's total. It is the common case today: OpenCost metric rows carry `total_cost` only. When one component is observed and the other is not, only the unsplit remainder of the total is apportioned to the missing one.
- A component split larger than the total is scaled down to the total.
- A baseline row aggregates many hours. When only some of them carry the CPU/RAM split (after an upgrade from 0.3.x, the hours re-aggregated from 0.3.x rows carry `total_cost` only), `cost_cpu` and `cost_ram` are the costs of the split hours `h_split`, and the total of the other hours `total_unsplit` is apportioned over their reserved quantities like the second case: `p_cpu(s) = (cost_cpu_s + total_unsplit × w'_cpu / (w'_cpu + w'_mem)) / q_cpu`, with `w'` computed over the unsplit hours only. The single upgrade hour holds both kinds of 5-minute rows: its hourly rollups (`combined_metrics_hourly`, `container_metrics_hourly`, `node_idle_metrics_hourly`, `workload_usage_hourly`) keep its `total_cost` and store NULL `cost_cpu` and `cost_ram`, so it is an unsplit hour. Summing the components there would cover part of the hour while the hour counted as split, and dilute the observed rates. Hours of the row whose observed total is zero count as zero cost (OpenCost said they cost nothing), in neither part: they add reserved quantity and no cost, so the projection stays bounded by the observed cost (BUG-15). When a non-zero unsplit total was apportioned, `detail` is `observed_components_partly_apportioned` with confidence at most 0.8, and `assumptions.unsplit_cost_hours_share` gives the share of the observed hours whose positive cost had no split (these hours and the samples with a total only).
- In both observed cases, the cost of the sample's CPU and RAM reservation is at most its observed total. A CPU cut and a memory cut on the same container therefore never project more than the container's observed cost, which is also the [workload deletion](#workload-deletion-zombie-pods-idle-namespaces) projection of that container (BUG-15).
- Rows without cost have `total_cost = 0` (`DEFAULT_COST`), and negative OpenCost costs are ignored, so these samples use the node prices.
- A `cost_source` label outside the known sources (for example `mixed` on hourly aggregates) is treated as `opencost` and kept in `assumptions.observed_cost_sources` (and in the node price `detail`, e.g. `observed:mixed`).

## Projection rules (`ImpactModel.project`)

All window totals are annualized:

```text
annual = window_total × 8760 / window_hours
```

### Annualization and observed coverage

The window totals of a target are sums over its samples, and a target has samples only since it was first observed. `window_hours` is therefore the target's **observed coverage**, not the scan's lookback:

```text
coverage     = window_end − first_observed(target)        # capped at the lookback
window_hours = max(coverage, MIN_COVERAGE_HOURS)          # MIN_COVERAGE_HOURS = 1
```

- `first_observed` is the first rolled hour of the target's usage summary (bounded scans: a container target reads its container summary, then the pod-level one; a namespace reads its earliest target). For contexts built from raw metric rows it is the earliest row timestamp. Rollup baseline rows are aggregates whose timestamp is the last hour they cover, so they are never used to guess a coverage.
- `window_end` is the end of the scan window. When it, or the first observation, is unknown, the coverage is the whole lookback (basis `lookback`), as in model 1.x: coverage is never guessed short, which would inflate the projection. When no lookback is known either, `window_hours` is the observed span of the samples.
- Gaps after the first observation stay in the coverage: a CronJob that runs one hour a day is annualized at one hour a day, and a workload that stopped is not extrapolated as if it still ran.
- **Minimum coverage.** A target observed for a few minutes is annualized as if observed for one hour (`assumptions.minimum_coverage_applied`), never from a single scrape.
- **Short coverage.** Below one week (`SHORT_COVERAGE_HOURS = 168`), and shorter than the lookback, weekly cycles are not seen yet. The projection records `short_coverage: true` and a note in `assumptions.notes`, and its `confidence` is multiplied by `max(coverage / 168, 0.25)`.
- The projection records `lookback_hours`, `coverage_hours`, `coverage_basis` (`target_summary`, `samples` or `lookback`) and `first_observed_at`.

Before model 2.0.0 the lookback was the denominator for every target. A workload observed for 2 hours inside a 14-day lookback was annualized as if those 2 hours were spread over 336 hours: for a workload allocated 0.05 per hour, a deletion projected 2 × 0.05 × 8760 / 336 = **2.61** per year instead of 0.05 × 8760 = **438** per year (168 times too low).

### Container requests (rightsizing; native analyzers and VPA)

The change moves a container request from `current` to `proposed`, per resource. For each sample `s`, with reservation `max(request, usage)`:

```text
released_s(res) = max(current, usage_s) − max(proposed, usage_s)
share_s         = min(max(current, usage_s), A) − min(max(proposed, usage_s), A)     # CPU, capped at the node

Δcost      = Σ_s released_s(cpu) × hours_s × p_cpu(s) + released_s(mem) × hours_s × p_ram(s)
ΔCO₂e_idle = Σ_s share_s × hours_s × P_min / A × PUE_s × intensity_s / 1000
ΔCO₂e_mem  = Σ_s share_s(mem) × hours_s × w_mem × PUE_s × intensity_s / 1000      # memory share, capped at A(mem)
ΔCO₂e_emb  = Σ_s share_s × hours_s × M_h / A
```

`p_cpu(s)` and `p_ram(s)` are the sample prices [anchored to the observed cost](#anchoring-to-the-observed-cost).

- CPU and memory deltas are computed per resource and are additive. A CPU recommendation and a memory recommendation on the same container can therefore never claim more than the container's allocated cost (its observed cost when OpenCost reports one). Before v1, each resource applied its request-cut ratio to the **total** pod cost (BUG-15).
- `current` is the latest request. When the request changed during the window, the projection prices the latest request, so it can differ from the cost observed earlier in the window.
- The idle and embodied share is capped at the node's allocatable capacity: a reservation larger than the node profile (typically the 1-vCPU default profile of an unknown node) never releases more than the whole node's idle power and embodied emissions. Cost is not capped.
- A CPU-only cut projects zero RAM savings, and the reverse.
- Request cuts do not change usage, so **dynamic** power is unchanged. Projected CO₂e is only the idle, memory and embodied share released (BUG-16): a memory cut releases memory power only (`co2e_breakdown_annual_g.memory_operational`).
- A target below observed usage releases capacity only down to that usage.

### Workload deletion (zombie pods, idle namespaces)

- Cost: each sample's observed allocated cost when it has one (OpenCost `totalCost`, split into components when available). Otherwise the modeled reservation cost.
- CO₂e: the sample's whole allocated share, that is idle, memory and embodied by reservation, dynamic by usage and its attributed GPU power. The reservation shares are capped at the node's allocatable cores and memory and the usage at its capacity, so one workload never carries more than the whole node.
- Realization factor (model 3.0.0, IMP-05): the pool factor of the nodes the samples ran on, as for a request cut. Deleting pods releases reserved capacity exactly like cutting their requests to zero: the bill only falls when a node goes away. Applied per part:

```text
expected_billable_cost_annual = factor × (cpu + ram + gpu + other) + (storage + lb + network)   # per-asset costs are billed whole
co2e_expected_annual_g        = factor × (idle + memory + embodied) + (dynamic + gpu)           # usage-based power is no longer consumed
```

  On fixed nodes (factor 0) a deletion's expected billable saving is 0 (or its per-asset costs) and its expected CO₂e is the dynamic and GPU part only. A request cut to the usage level and a deletion of the same idle workload differ only by those usage-based terms.

### Asset deletion (orphaned PersistentVolumes and LoadBalancers)

- Cost: the asset's current allocated rate. This is the OpenCost cost when reported, otherwise `STORAGE_COST_PER_GIB_MONTH` or `LOAD_BALANCER_COST_PER_MONTH` (recorded as `custom_rate`). GreenKube reads it from one `/allocation/compute` query over the last 7 days (`aggregate=namespace`, `accumulate=true`): a volume's cost is the sum of `cost + adjustment` of its entry in each allocation's `pvs` map (key `cluster=<id>:name=<pv>`, `__unmounted__` included), a LoadBalancer's cost the same sum in `lbAllocations`, matched on its exact `namespace/name`. The cost is annualized over the hours the asset existed in the window (`byteHours / capacity` for a volume, `hours` for a load balancer), not over the 7 days. An asset OpenCost knows at cost 0 is an observed zero: no cost saving, `cost_source` `opencost`, never the flat default (COST-03). Recommendation texts print the configured currency code.
- Realization factor: 1.
- CO₂e: **not modeled** (`co2e_modeled: false`), so `potential_savings_co2e_grams` stays empty.

### Replica and schedule changes (off-peak scale to zero)

Samples whose UTC hour falls in the scale-to-zero window are removed, priced like a workload deletion. The realization factor comes from the pool context, because replicas that leave a node only save money when the node goes away.

### Node pool consolidation (Karpenter)

A pool is recommended for consolidation when its average CPU request utilization is below `NODE_UTILIZATION_THRESHOLD`. With `n` nodes, CPU request utilization `u_cpu` and memory request utilization `u_mem` (against allocatable), it still needs `max(1, ceil(n × max(u_cpu, u_mem)))` nodes. The others are removable, and the model projects removing the cheapest ones (a conservative choice). When no node can be removed, the recommendation has no projection.

```text
Δcost = Σ_removed (cores × p_cpu + GiB × p_ram) × 8760
ΔCO₂e = Σ_removed ((P_min + P_mem + P_gpu_idle) / 1000 × PUE × intensity + M_h) × 8760     # dynamic power moves to other nodes
```

The realization factor is 1, because the consolidation is the action itself.

## Released capacity (WP-08.1)

Next to cost and CO₂e every projection reports the reserved capacity the change releases, in its own units. It needs no price, no OpenCost and no autoscaler, and it is the honest result on a fixed-capacity cluster, where the bill does not move until a node is removed.

- **Annual** (`impact.capacity_released`): `cpu_core_hours`, `mem_gib_hours`, `replica_hours` and `node_hours` per year, annualized like the cost over the target's observed coverage (so they inherit the coverage bias of the annualization above).
- **Instantaneous** (`released_capacity.instantaneous` on every recommendation read through the API): `(current request − proposed request) × replicas` in cores and GiB, per replica and in total. The replica count is the mean over the observed coverage (`replica_hours / 8760`).
- **Deletions**: `storage_gib` (orphaned PersistentVolume capacity) and `load_balancers` (count) for asset deletions only. A request change frees no storage. No network figure is reported.

Rules: released capacity is never converted to money and never added to a cost saving; it is labelled *projected* (here) or *measured* (`docs/methodology/savings-measurement.md`, "Released capacity"). An increase is a negative release and is never shown as a saving. The API returns it as `released_capacity` on each recommendation (`method: projected`, `period: year`, `units`, `direction`); the pull-request summary has a "Released capacity (projected)" table; the recommendation list and detail pages show it in a separate block.

## Realization factor

```text
expected_billable_cost_annual = allocated_cost_annual × realization_factor
co2e_expected_annual_g        = co2e_allocated_annual_g × realization_factor
```

Released requests only remove cost and emissions from the bill when nodes are removed. **RealizationContext** (B6, `greenkube.impact.realization`) derives the factor per node pool from what the cluster can do:

| Situation | Factor |
|---|---|
| No autoscaler (fixed nodes: allocated savings only) | 0.0 |
| Karpenter `WhenEmptyOrUnderutilized`, `WhenUnderutilized` (v1beta1) or `Balanced` | 0.8 |
| Karpenter `WhenEmpty` (nodes are removed only once empty; GreenKube heuristic) | 0.3 |
| Cluster Autoscaler (nodes outside Karpenter pools) | 0.6 |
| Karpenter disruption budget of `0` nodes, always active, for consolidation | 0.0 |
| Any of the above with blockers | × 0.5 |

Blockers are pods annotated `karpenter.sh/do-not-disrupt: "true"` (or the legacy `karpenter.sh/do-not-evict`) or `cluster-autoscaler.kubernetes.io/safe-to-evict: "false"`, nodes annotated `karpenter.sh/do-not-disrupt` or `cluster-autoscaler.kubernetes.io/scale-down-disabled`, PodDisruptionBudgets with `disruptionsAllowed: 0` selecting pods of the pool, a scheduled zero disruption budget, a Karpenter `DisruptionBlocked` event on the pool (its NodePool, one of its NodeClaims or nodes), a Cluster Autoscaler that reports itself unhealthy, and Karpenter metrics showing no consolidation decision over 7 days while nodes are eligible (consolidating pools only). Karpenter `Unconsolidatable` events are listed in the reasons without changing the factor.

Inputs and their sources:

- the Cluster Autoscaler status ConfigMap `kube-system/cluster-autoscaler-status`. Its presence means the autoscaler runs; its health is parsed defensively from the text or YAML status;
- Karpenter NodePools (`spec.disruption.consolidationPolicy` and `budgets`) and the `karpenter.sh/nodepool` label of nodes;
- when Karpenter runs, NodeClaims (claim → pool) and the `DisruptionBlocked`/`Unconsolidatable` events (core `events`, `list`); events that cannot be read are reported as such, never as "no blocker";
- when Karpenter runs and `PROMETHEUS_URL` is set, Karpenter's Prometheus metrics over the last 7 days: `sum by (reason) (increase(karpenter_voluntary_disruption_decisions_total[7d]))`, `sum by (reason) (karpenter_voluntary_disruption_eligible_nodes)` and `sum by (nodepool) (increase(karpenter_nodepools_cost_total[7d]))`. Consolidation decisions (`reason` `underutilized` or `empty`) are listed in the reasons of consolidating pools; no consolidation decision while nodes are eligible is a blocker of every consolidating pool (factor x 0.5); the pool cost increase is recorded as `observed_cost` of the pool. A metric Prometheus does not export changes nothing (never read as zero); a Prometheus that cannot be queried is noted as "Karpenter metrics could not be read". The decision and eligible-node metrics are cluster-wide (they carry no `nodepool` label), so one stalled pool halves the factor of every consolidating pool; and the decisions counter only exists after Karpenter's first decision, so a Karpenter that never consolidated reads as "not exported" (no blocker) rather than as zero.
- running pods, nodes and PodDisruptionBudgets.

The controller reads them from its collection cycle, at most every 10 minutes, inside the cluster only (the chart's ClusterRole already grants these reads). A projection uses the hours-weighted factor of the pools its samples ran on; nodes the context does not know use the Cluster Autoscaler (or fixed-node) pool. The projection records the factor, `realization_factor_source: realization_context`, `realization_reasons` (rendered with the recommendation evidence) and the pools used (`realization_pools`).

- `IMPACT_REALIZATION_FACTOR`, when set, overrides the context for every projection (`realization_factor_source: config:IMPACT_REALIZATION_FACTOR`).
- `IMPACT_AUTOSCALER_HINT` (`none`, `cluster_autoscaler`, `karpenter`; IMP-12) declares the autoscaler of the nodes outside Karpenter pools when detection does not see it (a Cluster Autoscaler run with another `--status-config-map-name` or namespace, a managed autoscaler that publishes neither signal). A detected Cluster Autoscaler wins; otherwise the hinted pool gets 0.6 (Cluster Autoscaler) or 0.8 (Karpenter), halved with blockers, and the reason names the hint.
- Each resolved context is stored (table `realization_context`, migration 0141, IMP-12). A recommendation scan in a process that has not observed the cluster yet (after a restart, Prometheus down) restores the stored context when it is younger than 24 hours; its pools' reasons say it was restored. The next refresh still reads the cluster.
- Before the controller has observed the cluster and without a stored context (or outside a cluster), the factor is **1.0** with `realization_factor_source: realization_context:unobserved`, so expected savings equal allocated savings, as in model 1.0.0.
- When the Cluster Autoscaler status or the Karpenter NodePools cannot be read (RBAC, timeout; a missing ConfigMap or CRD is not an error), the affected pools are never treated as "no autoscaler": they keep their previous observation, or the unobserved factor 1.0 before any, and the reason says the input could not be read.
- Workload deletions take the pool factor for their reserved parts (model 3.0.0, see [Workload deletion](#workload-deletion-zombie-pods-idle-namespaces)); asset deletions (orphaned PersistentVolumes and LoadBalancers, billed per asset) keep a factor of 1, and node-pool consolidation keeps 1 when the consolidation is the action itself.

## Carbon price and value

```text
value_annual = expected_billable_cost_annual + CARBON_PRICE_PER_TONNE × co2e_expected_annual_g / 1e6
```

`CARBON_PRICE_PER_TONNE` defaults to **100** currency units per tonne. This is an internal shadow price (roadmap decision D-2), in the range of EU ETS prices. Set it to 0 to value cost only. Ranking (roadmap I1) will use `value_annual`.

## Confidence

`confidence` measures how trustworthy the projection's **inputs** are, not how risky the change is. It is the average of the price-tier confidences above, weighted by sample hours: a sample priced from its observed cost counts 0.9 (0.8 when apportioned from a total), and a sample priced at node prices counts its node's tier. Recommendation risk and confidence (`risk_level`, `confidence` on the recommendation) are computed separately.

## Provenance: the `impact` field

Every projected recommendation carries `impact` (persisted in `recommendation_history.impact`, migration 0046):

```json
{
  "model_version": "1.1.0",
  "kind": "container_resources",
  "method": "projected",
  "currency": "USD",
  "allocated_cost_annual": 74.22,
  "cost_breakdown_annual": {"cpu": 74.22, "ram": 0.0, "gpu": 0.0, "storage": 0.0, "network": 0.0, "lb": 0.0, "other": 0.0, "total": 74.22},
  "capacity_released": {"cpu_core_hours": 7919.04, "mem_gib_hours": 0.0, "replica_hours": 8760.0, "node_hours": 0.0},
  "expected_billable_cost_annual": 74.22,
  "realization_factor": 1.0,
  "co2e_modeled": true,
  "co2e_allocated_annual_g": 23629.48,
  "co2e_expected_annual_g": 23629.48,
  "co2e_breakdown_annual_g": {"idle_operational": 1029.48, "dynamic_operational": 0.0, "memory_operational": 0.0, "gpu_operational": 0.0, "embodied": 22600.0, "total": 23629.48},
  "carbon_price_per_tonne": 100.0,
  "value_annual": 76.58,
  "confidence": 0.8,
  "assumptions": {
    "allocation_policy": "reservation_aware",
    "window_hours": 168.0,
    "annualization_factor": 52.14,
    "unit_prices": [{"cpu_core_hour": 0.009372, "ram_gib_hour": 0.001256, "currency": "USD", "source": "opencost", "detail": "observed_total_apportioned"}],
    "realization_factor_source": "realization_context:unobserved",
    "realization_reasons": ["The cluster's autoscalers have not been observed yet: expected savings equal allocated"],
    "co2e_basis": "idle_and_embodied_share_released",
    "pue": 1.3,
    "grid_intensity_g_per_kwh": 100.0,
    "power_sources": ["default_profile"],
    "observed_cost_sources": {"opencost": 168},
    "change": {"cpu_request_cores": [1.0, 0.096]}
  }
}
```

This example is a CPU cut from 1000m to 96m for one replica observed hourly for 7 days (usage 80m, 512 MiB of memory requested). OpenCost charged the container 0.01 per hour, with no CPU/RAM split. That total is apportioned with the default rate weights: 94% goes to the 1-core CPU reservation, hence 0.009372 per core-hour. The node appears in the metric rows but not in the node snapshot, so the default 1-vCPU power profile applies. Without OpenCost (`total_cost = 0`), the same cut is priced at the default rates: 250.33 per year with confidence 0.5. A node that is also absent from the metric rows is flagged `unknown_node_profile`.

Model 2.0.0 adds `lookback_hours`, `coverage_hours`, `coverage_basis` and `first_observed_at` to `assumptions` (and `short_coverage`, `notes` when the coverage is short), and three top-level fields described in [CO₂e components](#co2e-components-and-component-matched-comparison):

```json
  "co2e_components_annual_g": {"operational_energy": 0.0, "idle_share": 1029.48, "embodied": 22600.0},
  "co2e_measurable_components": ["operational_energy", "idle_share"],
  "co2e_measurable_expected_annual_g": 1029.48
```

The API v1 fields stay compatible:

- `potential_savings_cost = expected_billable_cost_annual`;
- `potential_savings_co2e_grams = co2e_expected_annual_g`, or empty when `co2e_modeled` is false.

## CO₂e components and component-matched comparison

The same model produces the projection and the measured rows (product principle "same model for estimate and measurement"). A projection splits its CO₂e into three components:

| Component | Breakdown fields | Allocated by |
|---|---|---|
| `operational_energy` | `dynamic_operational` + `memory_operational` + `gpu_operational` | CPU usage, memory reservation, DCGM attribution |
| `idle_share` | `idle_operational` | CPU reservation |
| `embodied` | `embodied` | CPU reservation |

Stored metric rows carry the operational CO₂e (`co2e_grams`: operational energy and idle share, allocated by the same model) and the embodied emissions apart (`embodied_co2e_grams`). Change measurement (`greenkube.verify`) reads `co2e_grams` and, in baseline snapshots frozen since embodied emissions are measured, `embodied_co2e_grams`: it **sees `operational_energy` and `idle_share`, plus `embodied` when both windows carry it** (the estimate lists the components it measured).

**Rule:** a measured value is only ever compared with the projected value of the components the measurement sees. It must also be on the same basis: stored rows are allocated, so a change measurement is compared with the **allocated** value of the measurable components (`measurable_co2e_projection(..., allocated=True)`, and `allocated_cost_annual` for cost), never with a realization-scaled value (projected ≠ allocated ≠ realized). `co2e_measurable_expected_annual_g` is the expected (billable) value of those components (the measurable components × the realization factor, the basis of `co2e_expected_annual_g`), reported for ranking; `greenkube.impact.comparison.measurable_co2e_projection` derives either value from any stored projection, including projections written before model 2.0.0 (from `co2e_breakdown_annual_g`). Without a projection that models CO₂e there is nothing to compare with, and no projection accuracy is reported (fail closed).

In change measurement:

- the `co2e_g` quantity is the workload's operational CO₂e per hour. Because idle and memory power are allocated by reservation, request changes move it, so it is claimed (`claimed_annualized`, `measured_co2e_saved_grams`) when the change is realized **and** its own 90% interval shows a saving;
- its `projected_annual` and `projection_accuracy` use the projection of the measured components only: the measurable projection, or the total with embodied emissions when the measurement carries them;
- it is split between the recommendations of a change by their measurable projections (equal shares when none was projected), so a CPU and a memory recommendation on the same container never both claim the whole CO₂e saving;
- a measurement whose snapshot predates embodied measurement compares operational CO₂e only and never claims the projected embodied share.

## SCI mapping

The Software Carbon Intensity specification (ISO/IEC 21031) defines `SCI = (E × I + M) per R`.

| SCI term | Impact Model |
|---|---|
| `E` (energy) | Allocated energy: dynamic CPU by usage, idle CPU and memory by reservation, attributed GPU, times PUE |
| `I` (intensity) | Grid intensity of the row's electricity zone and hour (Electricity Maps or Wattnet, with zone defaults) |
| `M` (embodied) | Boavizta manufacturing emissions × reservation share × time / lifespan |
| `R` (functional unit) | Not modeled: GreenKube reports per workload and per year |

## Limitations

- **No storage or network energy.** Deleting a volume or a load balancer projects cost only.
- **Memory power is constant per GiB.** It does not depend on memory activity. Embodied emissions are shared by CPU reservation only, as in Cloud Carbon Footprint.
- **GPU power needs the DCGM exporter.** Without it GPU power is not counted. GPU cost is not modeled, and GPU request changes are not projected.
- **The embodied lifespan is fixed** (4 years by default). Boavizta values are per instance and depend on its own assumptions.
- **Kepler** (roadmap B7), when added, may only provide dynamic CPU power, never idle.
- **Allocatable = capacity** until kubelet allocatable is collected. The system-reserved slice is therefore part of `__idle__`.
- **Marginal pricing.** Projections price released capacity at the node's unit rate. On an over-committed node, the measured allocation change can be smaller.
- **List prices.** Catalogs contain on-demand list prices with no discounts, savings plans or spot prices. Observed OpenCost costs take precedence when available.
- **Unsplit observed totals.** OpenCost's `totalCost` can include volume, network and load-balancer cost. While rows carry only the total, the apportioning attributes those to CPU and RAM. Request-cut projections can then be high by that amount, but never above the observed total.
- **Annualization assumes the observed coverage repeats.** Short or unusual coverage extrapolates its pattern; coverage below one week is flagged and lowers the confidence.
- **Embodied emissions are measured by allocation.** Change measurement measures the embodied CO₂e allocated by reservation (Boavizta values), not a physical quantity; older snapshots compare operational CO₂e only.
- **Rows written before model 1.1.0** (`model_version` empty) follow the previous usage-based policy until they are recomputed. Hourly aggregates are never recomputed.
- **Realization is a heuristic.** It does not yet measure pool headroom or the node-hours actually removed (`realize()`, roadmap C).

## Recalculation (B8)

```bash
greenkube recompute --since 7d            # rows written by another model version
greenkube recompute --since 2026-01-01T00:00:00Z --until 2026-01-08T00:00:00Z --all
greenkube recompute --since 24h --dry-run
```

`greenkube recompute` re-allocates **raw** rows still within retention with the current model, from what each row recorded: requests, usage, node, PUE, grid intensity, observed costs and attributed GPU power. Node power profiles, capacities and embodied emissions come from the node snapshots. Each node and step is recomputed as a whole and its `__idle__` row rewritten, so conservation holds. Container rows are re-split from their pod. Observed OpenCost costs and the stored intensity are kept. Hourly aggregates keep their original version. By default only rows whose `model_version` differs from the current one are recomputed; `--all` recomputes every row in the range.

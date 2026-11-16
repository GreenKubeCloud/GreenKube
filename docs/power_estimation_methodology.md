# Power Estimation Methodology for OVH and Scaleway

Since Cloud Carbon Footprint (CCF) does not provide default estimation constants for other cloud providers than AWS, GCP and Azure, we have derived provider-level estimates using the CCF methodology for "unknown micro-architectures".

## Node power and its allocation

GreenKube estimates node power from three components (Impact Model `1.1.0` and later, see [Impact Model methodology](methodology/impact-model.md)). For a collection step of `H` hours:

```text
node_utilization = min(total_node_cpu_cores / node_vcores, 1)
cpu_power_watts    = min_watts + node_utilization × (max_watts - min_watts)     # LinearCpuPowerModel
memory_power_watts = node_memory_gib × POWER_MEMORY_W_PER_GIB                    # MemoryPowerModel, 0.392 W/GiB
gpu_power_watts    = Σ GPUs: DCGM_FI_DEV_POWER_USAGE, else TDP model             # GpuPowerModel
```

Stored pod rows carry the **reservation-aware allocation** of that power (roadmap B2):

- idle CPU power (`min_watts`) and embodied emissions are shared by CPU **reservation** `max(request, usage) / node cores`;
- dynamic CPU power (`node_utilization × (max_watts − min_watts)`) is shared by CPU **usage**;
- memory power is shared by memory reservation `max(request, usage) / node memory`;
- each GPU's power goes to the pod DCGM attributes it to.

What no pod reserves (unreserved cores and memory, GPUs without a pod) is written to an explicit `__idle__` row per node and step (`node_idle_metrics`), so pod rows plus the idle row add up to the node's power, embodied emissions and modeled cost. Before this, 100% of node power was spread over pods by CPU use while cost excluded idle capacity (BUG-19).

### Memory power

`MemoryPowerModel` applies the Cloud Carbon Footprint memory coefficient, **0.392 W per GiB** (`POWER_MEMORY_W_PER_GIB`, Helm `impact.memoryWattsPerGiB`), to the node's memory capacity from the node snapshot. An m5.large (8 GiB) fully reserved draws about 3.1 W of memory power on top of its CPU profile. When the node memory capacity is unknown, only the reserved memory is counted.

### GPU power (DCGM)

When the NVIDIA DCGM exporter is scraped by Prometheus, GreenKube reads, per step:

- `avg_over_time(DCGM_FI_DEV_POWER_USAGE[step])`: the measured board power of each GPU;
- `avg_over_time(DCGM_FI_DEV_GPU_UTIL[step])`: the utilization, stored on the pod row as `gpu_usage_millicores` (1000 = one fully used GPU).

A GPU with Kubernetes attribution (`namespace`/`pod` labels, or `exported_namespace`/`exported_pod` when the exporter is scraped without `honorLabels`) is charged to that pod (`gpu_power_watts` on the row). Unattributed GPUs are charged to the node's `__idle__` row. When DCGM reports utilization but no power for a GPU, its power is `idle + util × (TDP − idle)` from the bundled `data/gpu_profiles.json` (board power of common data-center GPUs; idle is 15% of TDP). Without the DCGM exporter, GPU power is not counted. A DCGM query error never fails a collection window: GPU power is missing for that window and a warning is logged.

Known instance profiles use the provider's per-vCPU constants multiplied by the instance vCPU count. The currently loaded provider constants are:

| Provider | Min Watts/vCPU | Max Watts/vCPU |
|---|---:|---:|
| AWS | 0.74 | 3.50 |
| GCP | 0.71 | 4.26 |
| Azure | 0.78 | 3.76 |
| OVH | 0.80 | 3.52 |
| Scaleway | 0.60 | 2.78 |

When the instance type has no bundled profile, the collector resolves the node profile in this order and records where it came from (`power_source`):

1. `instance_profile` — the instance type is in the bundled CCF profile data.
2. `node_capacity_derived` — the node's CPU capacity (from a historical node snapshot, the live node list, or an inferred `cpu-N` type) multiplied by the per-vCPU coefficients of the node's provider in the table above; when the provider is unknown, the average of the listed providers is used (0.73–3.56 W per vCPU). The row is flagged as estimated with the reason.
3. `default_profile` — no instance type and no capacity are known: `config.defaults.instance` (default: 1 vCPU, 1–10 W) is used. The row is flagged as estimated and the node is counted in `greenkube_unknown_power_profile_nodes`.

When cAdvisor series carry no `node` label, the node is resolved from kube-state-metrics' `kube_pod_info` before falling back to the default profile, so a missing label no longer collapses the whole cluster onto one 10 W pseudo-node.

For operational emissions, GreenKube converts joules to kWh (`joules / 3,600,000`) and multiplies by grid intensity and PUE. These are model-based estimates, not direct power-meter readings.

### Grid intensity freshness and provenance

Each metric row records where its grid intensity came from (`intensity_source`):

| Value | Meaning |
|---|---|
| `consolidated` | Final provider value no older than `INTENSITY_MAX_AGE` (default `6h`) |
| `realtime` | Provisional provider value no older than `INTENSITY_MAX_AGE`; the hourly recompute replaces it once the provider consolidates it |
| `zone_default` | No fresh value: the bundled annual value of the zone is used |
| `country_default` | No fresh value and the zone (a sub-zone such as `US-CAL-CISO` or `SE-SE3`) has no bundled value: its country's annual value is used |
| `stale` | Only a value older than `INTENSITY_MAX_AGE` exists and neither the zone nor its country has a default |
| `global_default` | Neither a value nor a zone or country default: `DEFAULT_INTENSITY` is used |

The bundled defaults (`src/greenkube/data/electricity_maps_default.csv`) are the annual mean intensity of each country for one calendar year (2024), with life-cycle emission factors, from [Ember's yearly electricity data](https://ember-energy.org/data/yearly-electricity-data/) (CC BY 4.0). `scripts/generate_zone_intensity_defaults.py` regenerates the table; the year is in every row. An annual mean ignores the hour-to-hour variation of the grid: set an Electricity Maps or Wattnet token for hourly values. Rows already stored with a default keep the value they were computed with.

Defaults are computed when metrics are calculated and are never written into
the intensity history, so a missing token or a provider outage cannot pass for
measured data. Defaults that earlier versions stored there
(`estimation_method = default_fallback`) are deleted by the upgrade and ignored
by every lookup. The hourly recompute refreshes pod **and** container rows of the
last 24 hours with the latest fresh values; a row that moves from a default to a
measured value also loses the "Carbon intensity uses the ..." entry of the
deprecated `estimation_reasons` (and `is_estimated` when that was its only
reason). Rows already compressed into hourly aggregates are not recomputed, so
keep `METRICS_COMPRESSION_AGE_HOURS` at 24 or more. `power_source` records whether the
node used a known instance profile (`instance_profile`), a profile derived from
its capacity (`node_capacity_derived`) or the generic default (`default_profile`).

## Methodology

The CCF methodology states:
> "When we don’t know the underlying processor micro-architecture, we use the average or median of all micro-architectures used by that cloud provider."

We applied this approach by:
1.  Identifying the CPU micro-architectures commonly used by OVH and Scaleway based on their public documentation and hardware specifications.
2.  Retrieving the specific Min/Max Watts per vCPU for these micro-architectures from the CCF dataset (derived from SPECpower benchmarks).
3.  Calculating the average Min and Max Watts across the identified micro-architectures for each provider.

## Data Sources

- **Hardware Info:** Public documentation from OVHcloud and Scaleway.
- **Power Constants:** Cloud Carbon Footprint Coefficients (`ccf-coefficients` repository).

## Calculations

### OVHcloud
**Identified Architectures:** Intel Haswell, Broadwell, Skylake, Cascade Lake; AMD EPYC Milan, Genoa.

| Micro-architecture | Min Watts | Max Watts |
|--------------------|-----------|-----------|
| Haswell            | 1.90      | 5.99      |
| Broadwell          | 0.71      | 3.54      |
| Skylake            | 0.64      | 4.05      |
| Cascade Lake       | 0.64      | 3.80      |
| AMD EPYC Milan     | 0.45      | 1.87      |
| AMD EPYC Genoa*    | 0.45      | 1.87      |

*\*Genoa values estimated using Milan as a proxy due to lack of specific data.*

**Average for OVH:**
- **Min Watts:** 0.80
- **Max Watts:** 3.52

### Scaleway
**Identified Architectures:** AMD EPYC 1st Gen (Naples), 2nd Gen (Rome), 3rd Gen (Milan); Intel Xeon Gold (Skylake/Cascade Lake).

| Micro-architecture | Min Watts | Max Watts |
|--------------------|-----------|-----------|
| AMD EPYC 1st Gen   | 0.82      | 2.55      |
| AMD EPYC 2nd Gen   | 0.47      | 1.64      |
| AMD EPYC 3rd Gen   | 0.45      | 1.87      |
| Intel Skylake      | 0.64      | 4.05      |
| Intel Cascade Lake | 0.64      | 3.80      |

**Average for Scaleway:**
- **Min Watts:** 0.60
- **Max Watts:** 2.78

## Usage
The OVH and Scaleway values are stored in `provider_power_estimates.csv` and are used with the bundled instance profiles for known instance types. An unknown instance type uses the capacity-derived profile described above, and only falls back to the configured generic profile when the node's CPU capacity is unknown.

## GHG Protocol Scope Classification

GreenKube classifies carbon emissions according to the [GHG Protocol Corporate Accounting and Reporting Standard](https://ghgprotocol.org/corporate-standard):

| GHG Scope | GreenKube field | Description |
|---|---|---|
| **Scope 2** (location-based estimate) | `co2e_grams` | Indirect emissions from purchased electricity — computed as `grid_intensity × energy_kWh × PUE`. Grid intensity is sourced from Electricity Maps (default) or optional Wattnet, with the configurable `DEFAULT_INTENSITY` fallback. |
| **Scope 3, Category 1** (purchased goods & services) | `embodied_co2e_grams` | Upstream hardware manufacturing emissions allocated to the pod by CPU reservation share (`max(request, usage)`; the unreserved share goes to the node's `__idle__` row) — sourced from the [Boavizta API](https://api.boavizta.org) and amortised over the hardware lifespan. A `greenkube.io/embodied-kg` node annotation takes precedence (`embodied_source=configured`). When Boavizta does not recognise the provider or instance type, the node gets `DEFAULT_EMBODIED_KG_PER_VCPU` (default **12 kg CO₂e per vCPU**, the Boavizta m5 family ratio) × cores (`fallback_per_vcpu`), or `DEFAULT_EMBODIED_EMISSIONS_KG` (100 kg) when its capacity is unknown (`fallback_default`); both are flagged estimated. The Boavizta `/v1/cloud/instance` endpoint returns the lifetime embedded GWP of the instance share (`impacts.gwp.embedded.value`, 24 kg for `aws/m5.large`). |
| **Scope 2 + Scope 3** | `total_co2e_grams` | Full pod carbon footprint — computed field on `CombinedMetric`, also exposed as `total_co2e_all_scopes` in API summary and timeseries responses. |

**Scope 1** (direct combustion) is not applicable for cloud/virtualised Kubernetes workloads and is therefore not tracked.

For annual analysis, `total_co2e_all_scopes` combines the estimated Scope 2 and Scope 3 figures. The data export (`GET /api/v1/report/export`) includes `co2e_grams` and `embodied_co2e_grams` separately in each row so their underlying estimates can be examined independently.

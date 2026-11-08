# Impact Model v1 — methodology

**Model version:** `1.0.0` (`greenkube.impact.IMPACT_MODEL_VERSION`) · **Code:** `src/greenkube/impact/` · **Roadmap:** B1–B4

The Impact Model is the single set of formulas GreenKube uses to say what a workload **is charged** (allocated cost and allocated CO₂e), what it **draws** (energy), and what a proposed change **releases**. Recommendation projections use it today. Measurement (the savings ledger) will use the same model, so that a projection and its later measurement can be compared.

This page is the reference for code review. When a formula below changes the numbers the model produces, the change must bump the model version.

## Status of this version

| Part | Status |
|---|---|
| Projections for analyzers and connectors (`ImpactModel.project`) | **In use.** Every native analyzer with a savings figure, plus the VPA and Karpenter connectors, calls the model. The projection is stored as `impact` on each recommendation. |
| Reservation-aware allocation (`ImpactModel.allocate`) | **Implemented and tested** as pure functions (golden and conservation tests). Not yet wired into metric collection, so stored metric rows still come from the previous pipeline. |
| Cost fallback chain (observed → catalog → custom rates) | **In use for projections.** Each sample is anchored to its own observed cost when the row has one (the OpenCost `total_cost` written today, or `cost_cpu`/`cost_ram` when present). The node-level observed tier activates when metric rows carry per-resource costs. |
| Realization factor | **Fixed by configuration** (`IMPACT_REALIZATION_FACTOR`, default `1.0`) until per-pool realization (RealizationContext, roadmap B6) lands. |
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
- embodied emissions per hour `M_h` (gCO₂e/h);
- unit prices `p_cpu` (per core-hour) and `p_ram` (per GiB-hour);
- PUE and grid intensity.

The **allocatable** capacity `A` is the denominator of reservation shares. Until kubelet allocatable is collected, `A` equals the node capacity, which matches how OpenCost computes idle cost.

### Power profile

The model selects the power profile like the energy estimator does:

1. **`instance_profile`**: the instance type is in `data/instance_power_profiles.csv`, which holds Cloud Carbon Footprint per-vCPU coefficients times vCPUs.
2. **`node_capacity_derived`**: the instance type is unknown but the node CPU capacity is known. The model uses `DEFAULT_INSTANCE_MIN_WATTS` and `DEFAULT_INSTANCE_MAX_WATTS` per `DEFAULT_INSTANCE_VCORES`, times the capacity.
3. **`default_profile`**: nothing is known about the node. The model uses the default instance profile, and the projection is flagged with `unknown_node_profile`.

### Embodied emissions

`M_h = GWP × 1000 / (lifespan_years × 8760)` gCO₂e per node-hour.

- `GWP` is the Boavizta manufacturing value stored on the node (`embodied_emissions_kg`), or `DEFAULT_EMBODIED_EMISSIONS_KG` (100 kg).
- The lifespan is `DEFAULT_HARDWARE_LIFESPAN_YEARS` (4 years).

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

The full fixture has three nodes and ten pods, including an over-committed node and a pod present for half the window. It is in `tests/impact/test_allocation.py`.

## Unit prices: the cost fallback chain

Prices are resolved at two levels:

1. **Per node**, the unit prices `p_cpu(node)` and `p_ram(node)` come from the first available tier (1 to 3) of the table below.
2. **Per sample**, a projection then anchors the price to the sample's own observed cost when the row has one (tier 0, see [Anchoring to the observed cost](#anchoring-to-the-observed-cost)).

The tier is recorded with every projection (`assumptions.unit_prices[].source` and `.detail`).

| Tier | `source` | How the price is obtained | Confidence |
|---|---|---|---|
| 0. Observed, per sample | `opencost` / `kubecost` (the row's `cost_source`) | The sample's observed cost divided by its reserved quantity (below). `detail` is `observed_components` or `observed_total_apportioned`. | 0.9, or 0.8 when apportioned from a total |
| 1. Observed, per node | `opencost` / `kubecost` | `Σ cost_cpu / Σ reserved core-hours` and `Σ cost_ram / Σ reserved GiB-hours` over the node's metric rows. Both resources must be observed with positive values. | 0.9 |
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
- In both observed cases, the cost of the sample's CPU and RAM reservation is at most its observed total. A CPU cut and a memory cut on the same container therefore never project more than the container's observed cost, which is also the [workload deletion](#workload-deletion-zombie-pods-idle-namespaces) projection of that container (BUG-15).
- Rows without cost have `total_cost = 0` (`DEFAULT_COST`), and negative OpenCost costs are ignored, so these samples use the node prices.
- A `cost_source` label outside the known sources (for example `mixed` on hourly aggregates) is treated as `opencost` and kept in `assumptions.observed_cost_sources` (and in the node price `detail`, e.g. `observed:mixed`).

## Projection rules (`ImpactModel.project`)

All window totals are annualized:

```text
annual = window_total × 8760 / window_hours
```

`window_hours` is the analysis window of the scan. When no window is known, it is the observed span of the samples.

### Container requests (rightsizing; native analyzers and VPA)

The change moves a container request from `current` to `proposed`, per resource. For each sample `s`, with reservation `max(request, usage)`:

```text
released_s(res) = max(current, usage_s) − max(proposed, usage_s)
share_s         = min(max(current, usage_s), A) − min(max(proposed, usage_s), A)     # CPU, capped at the node

Δcost      = Σ_s released_s(cpu) × hours_s × p_cpu(s) + released_s(mem) × hours_s × p_ram(s)
ΔCO₂e_idle = Σ_s share_s × hours_s × P_min / A × PUE_s × intensity_s / 1000
ΔCO₂e_emb  = Σ_s share_s × hours_s × M_h / A
```

`p_cpu(s)` and `p_ram(s)` are the sample prices [anchored to the observed cost](#anchoring-to-the-observed-cost).

- CPU and memory deltas are computed per resource and are additive. A CPU recommendation and a memory recommendation on the same container can therefore never claim more than the container's allocated cost (its observed cost when OpenCost reports one). Before v1, each resource applied its request-cut ratio to the **total** pod cost (BUG-15).
- `current` is the latest request. When the request changed during the window, the projection prices the latest request, so it can differ from the cost observed earlier in the window.
- The idle and embodied share is capped at the node's allocatable capacity: a reservation larger than the node profile (typically the 1-vCPU default profile of an unknown node) never releases more than the whole node's idle power and embodied emissions. Cost is not capped.
- A CPU-only cut projects zero RAM savings, and the reverse.
- Request cuts do not change usage, so **dynamic** power is unchanged. Projected CO₂e is only the idle and embodied share released (BUG-16).
- A target below observed usage releases capacity only down to that usage.

### Workload deletion (zombie pods, idle namespaces)

- Cost: each sample's observed allocated cost when it has one (OpenCost `totalCost`, split into components when available). Otherwise the modeled reservation cost.
- CO₂e: the sample's whole allocated share, that is idle and embodied by reservation plus dynamic by usage. The reservation share is capped at the node's allocatable cores and the usage at its capacity, so one workload never carries more than the whole node.
- Realization factor: 1.

### Asset deletion (orphaned PersistentVolumes and LoadBalancers)

- Cost: the asset's current allocated rate. This is the OpenCost cost when reported, otherwise `STORAGE_COST_PER_GIB_MONTH` or `LOAD_BALANCER_COST_PER_MONTH` (recorded as `custom_rate`).
- Realization factor: 1.
- CO₂e: **not modeled** (`co2e_modeled: false`), so `potential_savings_co2e_grams` stays empty.

### Replica and schedule changes (off-peak scale to zero)

Samples whose UTC hour falls in the scale-to-zero window are removed, priced like a workload deletion. The realization factor comes from the pool context, because replicas that leave a node only save money when the node goes away.

### Node pool consolidation (Karpenter)

A pool is recommended for consolidation when its average CPU request utilization is below `NODE_UTILIZATION_THRESHOLD`. With `n` nodes, CPU request utilization `u_cpu` and memory request utilization `u_mem` (against allocatable), it still needs `max(1, ceil(n × max(u_cpu, u_mem)))` nodes. The others are removable, and the model projects removing the cheapest ones (a conservative choice). When no node can be removed, the recommendation has no projection.

```text
Δcost = Σ_removed (cores × p_cpu + GiB × p_ram) × 8760
ΔCO₂e = Σ_removed (P_min / 1000 × PUE × intensity + M_h) × 8760     # dynamic power moves to other nodes
```

The realization factor is 1, because the consolidation is the action itself.

## Realization factor

```text
expected_billable_cost_annual = allocated_cost_annual × realization_factor
co2e_expected_annual_g        = co2e_allocated_annual_g × realization_factor
```

Released requests only remove cost and emissions from the bill when nodes are removed. Until RealizationContext (B6) derives the factor per node pool, the model uses `IMPACT_REALIZATION_FACTOR` (default **1.0**, recorded as `realization_factor_source: config:IMPACT_REALIZATION_FACTOR`).

The default of 1.0 keeps `potential_savings_cost` in **allocated** terms. That is also what verification measures today: allocated cost before and after the change. B6 will use this heuristic:

| Situation | Factor |
|---|---|
| No autoscaler (fixed nodes) | 0.0 |
| Karpenter `WhenEmptyOrUnderutilized` or `Balanced` | 0.8 |
| Cluster Autoscaler | 0.6 |
| Any of the above with blockers (`do-not-disrupt`, PDBs) | × 0.5 |

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
  "model_version": "1.0.0",
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
  "co2e_breakdown_annual_g": {"idle_operational": 1029.48, "dynamic_operational": 0.0, "embodied": 22600.0, "total": 23629.48},
  "carbon_price_per_tonne": 100.0,
  "value_annual": 76.58,
  "confidence": 0.8,
  "assumptions": {
    "allocation_policy": "reservation_aware",
    "window_hours": 168.0,
    "annualization_factor": 52.14,
    "unit_prices": [{"cpu_core_hour": 0.009372, "ram_gib_hour": 0.001256, "currency": "USD", "source": "opencost", "detail": "observed_total_apportioned"}],
    "realization_factor_source": "config:IMPACT_REALIZATION_FACTOR",
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

The API v1 fields stay compatible:

- `potential_savings_cost = expected_billable_cost_annual`;
- `potential_savings_co2e_grams = co2e_expected_annual_g`, or empty when `co2e_modeled` is false.

## SCI mapping

The Software Carbon Intensity specification (ISO/IEC 21031) defines `SCI = (E × I + M) per R`.

| SCI term | Impact Model |
|---|---|
| `E` (energy) | Allocated energy: dynamic by usage plus idle by reservation, times PUE |
| `I` (intensity) | Grid intensity of the row's electricity zone and hour (Electricity Maps or Wattnet, with zone defaults) |
| `M` (embodied) | Boavizta manufacturing emissions × reservation share × time / lifespan |
| `R` (functional unit) | Not modeled: GreenKube reports per workload and per year |

## Limitations

- **No storage or network energy.** Deleting a volume or a load balancer projects cost only.
- **Memory power is not modeled yet** (roadmap B5). Memory request cuts project zero CO₂e, and embodied emissions are shared by CPU reservation only, as in Cloud Carbon Footprint.
- **No GPU model.** GPU cost and power are not projected.
- **The embodied lifespan is fixed** (4 years by default). Boavizta values are per instance and depend on its own assumptions.
- **Kepler** (roadmap B7), when added, may only provide dynamic CPU power, never idle.
- **Allocatable = capacity** until kubelet allocatable is collected. The system-reserved slice is therefore part of `__idle__`.
- **Marginal pricing.** Projections price released capacity at the node's unit rate. On an over-committed node, the measured allocation change can be smaller.
- **List prices.** Catalogs contain on-demand list prices with no discounts, savings plans or spot prices. Observed OpenCost costs take precedence when available.
- **Unsplit observed totals.** OpenCost's `totalCost` can include volume, network and load-balancer cost. While rows carry only the total, the apportioning attributes those to CPU and RAM. Request-cut projections can then be high by that amount, but never above the observed total.
- **Annualization assumes the observed window repeats.** Short or unusual windows extrapolate their pattern.
- **Allocation is not yet wired into collection.** Until it is, the stored `co2e_grams` of metric rows still follow the previous usage-based policy. Projections do not read them.

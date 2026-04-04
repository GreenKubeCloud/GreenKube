# GreenKube Sustainability Score — Methodology

## Overview

The **GreenKube Sustainability Score** is a composite metric ranging from **0 to 100**, where **100 represents a perfectly optimized, sustainable cluster**. It is the single "golden signal" for Kubernetes sustainability, exposed as the Prometheus gauge `greenkube_sustainability_score`.

The score aggregates seven independent dimensions that together capture the full picture of a cluster's environmental and operational efficiency. Each dimension is scored from 0 to 100 internally, then combined via a weighted average.

---

## Dimensions & Weights

| #  | Dimension                  | Weight | What it measures                                                |
|----|----------------------------|--------|-----------------------------------------------------------------|
| 1  | Resource Efficiency        | 25%    | How well CPU and memory requests match actual usage             |
| 2  | Carbon Efficiency          | 20%    | Effective CO₂ cost per kWh of compute: grid intensity × PUE    |
| 3  | Waste Elimination          | 15%    | Absence of zombie pods and idle namespaces                      |
| 4  | Node Efficiency            | 15%    | Utilization and consolidation of infrastructure nodes           |
| 5  | Scaling Practices          | 10%    | Use of autoscaling and off-peak scaling                         |
| 6  | Carbon-Aware Scheduling    | 10%    | Workloads shifted to low-carbon time windows                    |
| 7  | Stability                  | 5%     | Pod stability (low restart count)                               |

**Total: 100%**

The weights reflect the relative impact each dimension has on real-world sustainability. Resource efficiency and carbon efficiency dominate because they directly drive emissions. Stability is weighted least because it is an indirect signal.

---

## Dimension Details

### 1. Resource Efficiency (25%)

**Goal:** Pods should request only what they actually consume. Overprovisioning wastes energy and money.

**Inputs:**
- `cpu_request` and `cpu_usage_millicores` per pod
- `memory_request` and `memory_usage_bytes` per pod

**Calculation:**
For each pod with non-zero requests and usage data:
- `cpu_ratio = min(avg_cpu_usage / cpu_request, 1.0)` — capped at 1.0 (over-usage is not penalized here)
- `memory_ratio = min(avg_memory_usage / memory_request, 1.0)` — same logic
- `pod_efficiency = (cpu_ratio + memory_ratio) / 2`

The dimension score is the **energy-weighted average** of all pod efficiencies:
```
resource_score = Σ(pod_efficiency_i × joules_i) / Σ(joules_i) × 100
```
Energy-weighting ensures that large, energy-hungry workloads have more impact on the score than tiny pods.

**Edge cases:**
- Pods with zero requests or no usage data are excluded from the calculation.
- If no pods have valid data, the dimension scores a neutral **50**.

---

### 2. Carbon Efficiency (20%)

**Goal:** Answer "how much CO₂ do you actually emit per kWh of compute, compared to the theoretical perfect setup (PUE=1.0, zero-carbon renewable grid)?"

The key insight is that the datacenter's **PUE (Power Usage Effectiveness)** is a direct multiplier on carbon emissions: for every Joule of compute work, a datacenter with PUE=1.5 consumes 50% more electricity — and therefore emits 50% more CO₂ — than a perfectly efficient one with PUE=1.0. This must be factored into the score alongside grid carbon intensity.

**Inputs:**
- `grid_intensity` (gCO₂e/kWh) per pod measurement
- `pue` (Power Usage Effectiveness) per pod measurement
- `joules` per pod measurement

**Calculation:**

```
effective_intensity_i = grid_intensity_i × pue_i
weighted_effective_intensity = Σ(effective_intensity_i × joules_i) / Σ(joules_i)
carbon_efficiency_score = max(0, 100 × (1 − weighted_effective_intensity / 800))
```

The **800 gCO₂e/kWh ceiling** represents the worst-case dirty grid at PUE=1.0 (heavy coal). Any combination of grid intensity and PUE that yields an effective intensity ≥ 800 scores 0.

| Grid Intensity | PUE  | Effective (g×PUE) | Score |
|----------------|------|-------------------|-------|
| 0 gCO₂/kWh    | 1.0  | 0                 | 100   |
| 50 gCO₂/kWh   | 1.0  | 50                | ~94   |
| 200 gCO₂/kWh  | 1.0  | 200               | ~75   |
| 200 gCO₂/kWh  | 1.5  | 300               | ~63   |
| 200 gCO₂/kWh  | 2.0  | 400               | ~50   |
| 400 gCO₂/kWh  | 1.0  | 400               | ~50   |
| 400 gCO₂/kWh  | 1.5  | 600               | ~25   |
| 600 gCO₂/kWh  | 1.0  | 600               | ~25   |
| 800+ gCO₂/kWh | 1.0  | 800+              | 0     |

**Why PUE matters:** A cluster running on a relatively clean grid (200 gCO₂/kWh) but hosted in an inefficient datacenter (PUE=2.0) has the same effective carbon footprint as a cluster on a dirtier grid (400 gCO₂/kWh) in a modern, efficient datacenter (PUE=1.0). The score treats them identically — correctly — because the actual CO₂ per kWh of compute is what matters.

**Edge cases:**
- If `pue` is missing or < 1.0 (invalid), it defaults to 1.0 (ideal).
- If no energy data is available, the dimension scores a neutral **50**.

---

### 3. Waste Elimination (15%)

**Goal:** No zombie pods, no idle namespaces. Every running workload should serve a purpose.

**Inputs:**
- Zombie pod detection: pods with cost > `ZOMBIE_COST_THRESHOLD` but energy < `ZOMBIE_ENERGY_THRESHOLD`
- Idle namespace detection: namespaces with total energy < `IDLE_NAMESPACE_ENERGY_THRESHOLD` but cost > 0

**Calculation:**
```
zombie_ratio = count(zombie_pods) / count(total_pods)
idle_ns_ratio = count(idle_namespaces) / count(total_namespaces)
waste_score = (1 − zombie_ratio) × 0.7 + (1 − idle_ns_ratio) × 0.3) × 100
```

The zombie ratio is weighted more heavily (70%) because zombie pods are a more actionable waste signal than idle namespaces.

**Edge cases:**
- If there are 0 pods, the score defaults to **100** (no waste in an empty cluster).
- System namespaces (`kube-system`, etc.) are excluded from idle namespace counting (following recommender's `RECOMMEND_SYSTEM_NAMESPACES` setting).

---

### 4. Node Efficiency (15%)

**Goal:** Nodes should be well-utilized. Overprovisioned and underutilized nodes waste energy.

**Inputs:**
- Per-node CPU utilization from pod-level metrics aggregated by node
- Pod count per node

**Calculation:**
For each node:
- `node_util = total_cpu_usage_on_node / node_cpu_capacity`
- Score the node based on utilization:
  - Below `NODE_UTILIZATION_THRESHOLD` (default 20%): heavily penalized
  - Between 20% and 70%: linearly scaled (optimal zone)
  - Above 70%: full marks (high utilization is good for sustainability)

```
node_score_i = min(node_util / 0.7, 1.0) × 100
node_efficiency_score = avg(node_score_i for all nodes)
```

**Edge cases:**
- If no node info is available, the dimension scores a neutral **50**.
- Nodes with 0 capacity are excluded.

---

### 5. Scaling Practices (10%)

**Goal:** Workloads should use autoscaling to avoid static overprovisioning, and should scale to zero during off-peak hours.

**Inputs:**
- Autoscaling candidates detected by the recommender (pods with CV > threshold and spike ratio > threshold, lacking HPA)
- Off-peak scaling candidates (pods idle for consecutive hours)

**Calculation:**
```
autoscale_penalty = count(autoscaling_candidates) / count(total_pods_with_data)
offpeak_penalty = count(offpeak_candidates) / count(total_pods_with_data)
scaling_score = (1 − (autoscale_penalty × 0.6 + offpeak_penalty × 0.4)) × 100
```

Autoscaling is weighted more (60%) because it addresses the most common pattern of static overprovisioning.

**Edge cases:**
- If fewer than 3 time-series data points exist per pod, the pod is skipped (insufficient data for variability analysis).
- If no pods have sufficient data, the dimension scores a neutral **50**.

---

### 6. Carbon-Aware Scheduling (10%)

**Goal:** Workloads should run during low-carbon intensity windows. Pods running during peak-intensity periods are penalized.

**Inputs:**
- Per-zone average carbon intensity
- Per-pod average carbon intensity vs. zone average

**Calculation:**
```
carbon_aware_pod_ratio = count(pods_running_during_high_intensity) / count(total_pods_with_zone_data)
carbon_aware_score = (1 − carbon_aware_pod_ratio) × 100
```

A pod is considered "running during high intensity" if its average grid intensity exceeds the zone average by more than `CARBON_AWARE_THRESHOLD` (default: 1.5×).

**Edge cases:**
- If no zone data is available, the dimension scores a neutral **50**.

---

### 7. Stability (5%)

**Goal:** Stable pods that don't restart unnecessarily avoid wasted boot-up energy and carbon.

**Inputs:**
- `restart_count` per pod

**Calculation:**
```
avg_restarts = mean(restart_count for all pods where restart_count is not None)
stability_score = max(0, 100 − avg_restarts × 10)
```

Each average restart costs 10 points. This means:
- 0 restarts → 100
- 5 restarts avg → 50
- 10+ restarts avg → 0

**Edge cases:**
- If no restart data is available, the dimension scores a neutral **50**.

---

## Final Score Computation

```
sustainability_score = Σ(dimension_score_i × weight_i)
```

The result is rounded to one decimal place and clamped to `[0, 100]`.

---

## Prometheus Metrics Exposed

| Metric                                          | Type  | Labels    | Description                                              |
|-------------------------------------------------|-------|-----------|----------------------------------------------------------|
| `greenkube_sustainability_score`                | Gauge | `cluster` | Composite sustainability score (0–100, higher is better) |
| `greenkube_sustainability_dimension_score`      | Gauge | `cluster`, `dimension` | Score per dimension (0–100)           |
| `greenkube_carbon_intensity_score`              | Gauge | `cluster` | Energy-weighted avg grid intensity (gCO₂e/kWh) — kept for backward compat |
| `greenkube_carbon_intensity_zone`               | Gauge | `cluster`, `zone` | Grid intensity per electricity zone     |

The `dimension` label takes values: `resource_efficiency`, `carbon_efficiency`, `waste_elimination`, `node_efficiency`, `scaling_practices`, `carbon_aware_scheduling`, `stability`.

---

## Design Decisions

1. **Energy-weighted averages** are used wherever possible so that high-consumption workloads dominate the score, reflecting their outsized environmental impact.
2. **Neutral fallback of 50** for dimensions without data ensures the score is not artificially inflated or deflated when data is missing. It represents "unknown" rather than "perfect" or "terrible".
3. **Weights are fixed** (not configurable) to keep the score comparable across clusters and organizations. This ensures that "a score of 80" means the same thing everywhere.
4. **Reuse of recommender logic**: the waste, scaling, and carbon-aware dimensions leverage the same detection thresholds from `config.py` as the recommendation engine, ensuring consistency.
5. **Backward compatibility**: `greenkube_carbon_intensity_score` and `greenkube_carbon_intensity_zone` are kept as separate metrics alongside the new composite score.

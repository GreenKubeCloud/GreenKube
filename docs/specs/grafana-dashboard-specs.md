# GreenKube Grafana Dashboard — Specification

> **Version:** 1.0 — April 2026  
> **Scope:** Design specification for the canonical GreenKube FinGreenOps Grafana dashboard. This document defines what the dashboard should contain, the rationale for each panel, and identifies metrics that must be added to make the dashboard complete.

---

## 1. Design Philosophy

The GreenKube dashboard serves **two audiences simultaneously**:

1. **Platform / SRE teams** — "Is our cluster healthy and efficient?"
2. **Sustainability / FinOps leads** — "What is our carbon and cost footprint, and where can we improve?"

**Principles:**
- **Progressive disclosure** — The most critical signal (Sustainability Score) appears first. Details are revealed row by row.
- **Actionability over aesthetics** — Every panel must either expose a problem or quantify an improvement opportunity.
- **Self-contained** — A user should be able to import the dashboard and immediately understand the full picture without reading docs.
- **Modularity** — Each panel uses a single, standalone PromQL query so users can copy individual panels into their own dashboards.

---

## 2. Variables (Dashboard Filters)

| Variable | Type | Query | Default |
|----------|------|-------|---------|
| `$cluster` | Label values | `label_values(greenkube_cluster_co2e_grams_total, cluster)` | All |
| `$namespace` | Label values | `label_values(greenkube_namespace_co2e_grams_total{cluster="$cluster"}, namespace)` | All |
| `$node` | Label values | `label_values(greenkube_node_info, node)` | All |

---

## 3. Dashboard Rows & Panels

### Row 0 — Sustainability Command Center *(always visible, collapsed=false)*

**Purpose:** A single-screen summary. Should answer "are we good, and where should we act?" in under 5 seconds. Every panel here is either an actionable signal or a direct financial/environmental consequence.

**Layout (suggested):** The Sustainability Score gauge takes up 1/4 of the width on the left. The remaining 3/4 is split into three sub-rows of stats.

**Sub-row A — Current footprint:**

| # | Panel title | Type | Metric / PromQL | Unit | Threshold |
|---|-------------|------|-----------------|------|-----------|
| 1 | **Sustainability Score** | Gauge (big) | `greenkube_sustainability_score{cluster="$cluster"}` | 0–100 | 🔴 <40 🟡 <70 🟢 ≥70 |
| 2 | **Total CO₂e (Scope 2)** | Stat | `greenkube_cluster_co2e_grams_total{cluster="$cluster"}` | g CO₂e | — |
| 3 | **Total CO₂e (Scope 3 Embodied)** | Stat | `greenkube_cluster_embodied_co2e_grams_total{cluster="$cluster"}` | g CO₂e | — |
| 4 | **Total Cloud Cost** | Stat | `greenkube_cluster_cost_dollars_total{cluster="$cluster"}` | $ | — |
| 5 | **Active Pods** | Stat | `greenkube_cluster_pod_count{cluster="$cluster"}` | pods | — |

**Sub-row B — GreenKube impact (realized savings):** *(requires new metrics — see §4.10–4.12)*

| # | Panel title | Type | Metric / PromQL | Unit | Threshold |
|---|-------------|------|-----------------|------|-----------|
| 6 | **CO₂e Saved by GreenKube** | Stat (green accent) | `greenkube_cluster_co2e_saved_grams_total{cluster="$cluster"}` | g CO₂e | 🟢 >0 |
| 7 | **Cost Saved by GreenKube** | Stat (green accent) | `greenkube_cluster_cost_saved_dollars_total{cluster="$cluster"}` | $ | 🟢 >0 |
| 8 | **Recommendations Implemented** | Stat | `greenkube_recommendations_implemented_total{cluster="$cluster"}` | count | 🟢 >0 |

> These three panels are GreenKube's "proof of value" strip. They answer the question *"what has this platform already achieved?"* and are essential for sustainability reports, FinOps reviews, and internal adoption conversations.

**Sub-row C — Where to act next:**

| # | Panel title | Type | Metric / PromQL | Unit | Threshold |
|---|-------------|------|-----------------|------|-----------|
| 9 | **Potential CO₂e Savings** | Stat | `sum(greenkube_recommendations_savings_co2e_grams)` | g CO₂e | 🟡 >0 |
| 10 | **Potential Cost Savings** | Stat | `sum(greenkube_recommendations_savings_cost_dollars)` | $ | 🟡 >0 |
| 11 | **#1 Namespace by CO₂e** | Stat | `topk(1, greenkube_namespace_co2e_grams_total{cluster="$cluster"})` — display label + value | g CO₂e | — |
| 12 | **#1 Namespace by Savings Potential** | Stat | `topk(1, greenkube_namespace_recommendation_savings_co2e_grams_total{cluster="$cluster"})` *(requires new metric — see §4.8)* — display label + value | g CO₂e | — |

**Rationale:** "Total Energy" is removed — it is implied by CO₂e and adds little actionable signal on its own. "Grid Carbon Intensity" and "Data Freshness" are contextual metrics moved to dedicated rows (Row 1 and Row 10) where they have more depth. The three sub-rows now tell a complete story: current footprint → what GreenKube has already achieved → where to act next.

#### Companion panel — Top 3 Namespaces by CO₂e *(placed immediately below the stat row)*

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 10 | **Top 3 Namespaces — CO₂e** | Bar gauge (horizontal) | `topk(3, greenkube_namespace_co2e_grams_total{cluster="$cluster"})` | g CO₂e |
| 11 | **Top 3 Namespaces — Cost** | Bar gauge (horizontal) | `topk(3, greenkube_namespace_cost_dollars_total{cluster="$cluster"})` | $ |
| 12 | **Top 3 Namespaces — Savings Potential** | Bar gauge (horizontal) | `topk(3, greenkube_namespace_recommendation_savings_co2e_grams_total{cluster="$cluster"})` *(requires new metric — see §4)* | g CO₂e |

These three bar gauges form the "where to act" half of the command center — immediately after seeing the scores, the operator knows which namespaces to open next.

---

### Row 1 — Carbon, Cost & Energy Trends *(time series)*

**Purpose:** Show how the cluster evolves over the selected time window. Useful for correlating deployments, incidents, or time-of-day effects with emission peaks.

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 9 | **CO₂e Over Time** | Time series | `greenkube_cluster_co2e_grams_total{cluster="$cluster"}` | g CO₂e |
| 10 | **Cloud Cost Over Time** | Time series | `greenkube_cluster_cost_dollars_total{cluster="$cluster"}` | $ |
| 11 | **Energy Consumption Over Time** | Time series | `greenkube_cluster_energy_joules_total{cluster="$cluster"} / 3600000` | kWh |
| 12 | **Grid Carbon Intensity by Zone** | Time series | `greenkube_carbon_intensity_zone{cluster="$cluster"}` (one series per `zone`) | gCO₂/kWh |

**Rationale:** Time series trends are essential for FinOps/sustainability reporting (CSRD/ESRS E1 requires trend data). The per-zone intensity line is the key signal for carbon-aware scheduling decisions.

---

### Row 2 — Sustainability Score Breakdown *(diagnostic view)*

**Purpose:** Decompose the composite score to identify which dimensions drag performance down.

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 13 | **Score by Dimension** | Bar gauge (horizontal) | `greenkube_sustainability_dimension_score{cluster="$cluster"}` (by `dimension`) | 0–100 |
| 14 | **Sustainability Score Over Time** | Time series | `greenkube_sustainability_score{cluster="$cluster"}` | 0–100 |
| 15 | **Estimation Coverage** | Gauge | `1 - greenkube_estimated_metrics_ratio` | 0–100 % |

> **Panel 15 rationale:** When estimated_metrics_ratio is high, the sustainability score and all CO₂ figures are less reliable. This panel makes data quality immediately visible.

**Dimension labels (x-axis for panel 13):**
`resource_efficiency`, `carbon_efficiency`, `waste_elimination`, `node_efficiency`, `scaling_practices`, `carbon_aware_scheduling`, `stability`

---

### Row 3 — Resource Efficiency *(waste detection)*

**Purpose:** The single biggest lever for sustainability improvement. Overprovisioned pods waste energy and money.

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 16 | **CPU Efficiency per Namespace** | Bar gauge | `sum by (namespace) (greenkube_pod_cpu_usage_millicores{cluster="$cluster", namespace=~"$namespace"}) / sum by (namespace) (greenkube_pod_cpu_request_millicores{cluster="$cluster", namespace=~"$namespace"})` | 0–100 % |
| 17 | **Memory Efficiency per Namespace** | Bar gauge | `sum by (namespace) (greenkube_pod_memory_usage_bytes{cluster="$cluster", namespace=~"$namespace"}) / sum by (namespace) (greenkube_pod_memory_request_bytes{cluster="$cluster", namespace=~"$namespace"})` | 0–100 % |
| 18 | **CPU Efficiency per Pod (Top 20 worst)** | Table | `sort_desc(greenkube_pod_cpu_efficiency_ratio{cluster="$cluster", namespace=~"$namespace"})` *(requires new metric — see §4)* | ratio |
| 19 | **Memory Efficiency per Pod (Top 20 worst)** | Table | `sort_desc(greenkube_pod_memory_efficiency_ratio{cluster="$cluster", namespace=~"$namespace"})` *(requires new metric — see §4)* | ratio |

**Threshold for panels 16 & 17:** 🔴 <30% 🟡 <60% 🟢 ≥60%

---

### Row 4 — Namespace Analysis *(cost centre view)*

**Purpose:** Break down emissions and costs per team/application. Essential for internal chargeback and identifying high-impact namespaces.

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 20 | **CO₂e by Namespace** | Pie chart | `greenkube_namespace_co2e_grams_total{cluster="$cluster", namespace=~"$namespace"}` | g CO₂e |
| 21 | **Cost by Namespace** | Pie chart | `greenkube_namespace_cost_dollars_total{cluster="$cluster", namespace=~"$namespace"}` | $ |
| 22 | **Energy by Namespace** | Pie chart | `greenkube_namespace_energy_joules_total{cluster="$cluster", namespace=~"$namespace"} / 3600000` | kWh |
| 23 | **Namespace Summary** | Table | Join of CO₂e + cost + energy + pod count per namespace | — |

**Table 23 columns:** Namespace · Pods · CO₂e (g) · Embodied CO₂e (g) · Total CO₂e (g) · Energy (kWh) · Cost ($) · Avg PUE

---

### Row 5 — Top Emitters & Spenders *(accountability view)*

**Purpose:** Pinpoint the workloads with the highest environmental and financial footprint. Direct input for optimization conversations.

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 24 | **Top 15 Pods — CO₂e Emitters** | Bar gauge | `topk(15, greenkube_pod_co2e_grams{cluster="$cluster", namespace=~"$namespace"})` | g CO₂e |
| 25 | **Top 15 Pods — Cloud Cost** | Bar gauge | `topk(15, greenkube_pod_cost_dollars{cluster="$cluster", namespace=~"$namespace"})` | $ |
| 26 | **Top 15 Pods — Energy** | Bar gauge | `topk(15, greenkube_pod_energy_joules{cluster="$cluster", namespace=~"$namespace"} / 3600000)` | kWh |
| 27 | **Top 15 Pods — Embodied CO₂e (Scope 3)** | Bar gauge | `topk(15, greenkube_pod_embodied_co2e_grams{cluster="$cluster", namespace=~"$namespace"})` | g CO₂e |

---

### Row 6 — Node Analysis *(infrastructure efficiency)*

**Purpose:** Understand whether the underlying infrastructure is efficiently sized and utilized. Node-level waste is a systemic problem harder to fix but with the highest leverage.

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 28 | **Node CPU Allocation Ratio** | Bar gauge | `greenkube_node_cpu_allocated_millicores{node=~"$node"} / greenkube_node_cpu_capacity_millicores{node=~"$node"}` *(requires new metric — see §4)* | 0–100 % |
| 29 | **Node Memory Allocation Ratio** | Bar gauge | `greenkube_node_memory_allocated_bytes{node=~"$node"} / greenkube_node_memory_capacity_bytes{node=~"$node"}` *(requires new metric — see §4)* | 0–100 % |
| 30 | **Node Embodied Emissions** | Bar gauge | `greenkube_node_embodied_emissions_kg{node=~"$node"}` | kg CO₂e |
| 31 | **Node Info** | Table | `greenkube_node_info{node=~"$node"}` | info |

**Table 31 columns:** Node · Instance Type · Cloud Provider · Zone · Region · Architecture · CPU Capacity · Memory Capacity · Embodied CO₂e (kg)

---

### Row 7 — Network & Storage I/O *(hidden cost of data)*

**Purpose:** Network and storage I/O have real energy costs that are often invisible. This row surfaces them for the top consumers.

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 32 | **Top 10 Pods — Network Receive** | Time series | `topk(10, greenkube_pod_network_receive_bytes{cluster="$cluster", namespace=~"$namespace"})` | B/s |
| 33 | **Top 10 Pods — Network Transmit** | Time series | `topk(10, greenkube_pod_network_transmit_bytes{cluster="$cluster", namespace=~"$namespace"})` | B/s |
| 34 | **Top 10 Pods — Disk Read** | Time series | `topk(10, greenkube_pod_disk_read_bytes{cluster="$cluster", namespace=~"$namespace"})` | B/s |
| 35 | **Top 10 Pods — Disk Write** | Time series | `topk(10, greenkube_pod_disk_write_bytes{cluster="$cluster", namespace=~"$namespace"})` | B/s |

---

### Row 8 — Pod Stability *(reliability signal)*

**Purpose:** Pod restarts indicate instability that leads to wasted energy (cold starts, re-initialization). They are also a prerequisite for carbon-aware scheduling (you cannot shift unstable workloads).

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 36 | **Top 15 Restarting Pods** | Bar gauge | `topk(15, greenkube_pod_restart_count{cluster="$cluster", namespace=~"$namespace"})` *(requires new metric — see §4)* | restarts |
| 37 | **Restart Heatmap by Namespace** | Bar gauge | `sum by (namespace) (greenkube_pod_restart_count{cluster="$cluster", namespace=~"$namespace"})` *(requires new metric — see §4)* | restarts |

---

### Row 9 — Recommendations & Savings Potential *(call-to-action)*

**Purpose:** Quantify the value of acting on GreenKube's recommendations. This row directly translates findings into ROI figures.

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 38 | **Total Potential CO₂e Savings** | Stat | `sum(greenkube_recommendations_savings_co2e_grams)` | g CO₂e |
| 39 | **Total Potential Cost Savings** | Stat | `sum(greenkube_recommendations_savings_cost_dollars)` | $ |
| 40 | **Recommendations by Type** | Bar gauge | `greenkube_recommendations_total` (by `type`) | count |
| 41 | **Recommendations by Priority** | Pie chart | `sum by (priority) (greenkube_recommendations_total)` | count |
| 42 | **CO₂e Savings by Recommendation Type** | Bar gauge | `greenkube_recommendations_savings_co2e_grams` (by `type`) | g CO₂e |
| 43 | **Cost Savings by Recommendation Type** | Bar gauge | `greenkube_recommendations_savings_cost_dollars` (by `type`) | $ |

---

### Row 10 — GreenKube Self-Monitoring *(data quality)*

**Purpose:** Ensure the platform itself is healthy and data can be trusted.

| # | Panel title | Type | Metric / PromQL | Unit |
|---|-------------|------|-----------------|------|
| 44 | **Last Collection** | Stat | `greenkube_last_collection_timestamp_seconds` (display as time-ago) | s |
| 45 | **Metrics in Window** | Stat | `greenkube_metrics_total` | pods |
| 46 | **Estimated Metrics Ratio** | Gauge | `greenkube_estimated_metrics_ratio * 100` | % |
| 47 | **Active Namespaces** | Stat | `greenkube_cluster_namespace_count{cluster="$cluster"}` | count |

---

## 4. Missing Metrics — Gap Analysis

The following metrics are required by the dashboard above but **not currently exposed** by GreenKube's `/prometheus/metrics` endpoint. They must be implemented.

### 4.1 `greenkube_pod_restart_count`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `cluster`, `namespace`, `pod`, `node`, `region` |
| **Description** | Number of pod restarts (from Prometheus `kube_pod_container_status_restarts_total`) |
| **Status** | ✅ Data is already collected by `PrometheusCollector` (field `pod_restart_counts` in `PrometheusMetric`) and stored in `CombinedMetric.restart_count` — **but never exposed to Prometheus** |
| **Priority** | 🔴 High |

### 4.2 `greenkube_pod_cpu_efficiency_ratio`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `cluster`, `namespace`, `pod`, `node`, `region` |
| **Description** | `cpu_usage_millicores / cpu_request_millicores` (capped at 1.0). Null when request is 0. |
| **Status** | Derivable in PromQL but an explicit metric simplifies alerting and panel queries significantly |
| **Priority** | 🟡 Medium |

### 4.3 `greenkube_pod_memory_efficiency_ratio`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `cluster`, `namespace`, `pod`, `node`, `region` |
| **Description** | `memory_usage_bytes / memory_request_bytes` (capped at 1.0). Null when request is 0. |
| **Status** | Same as above — derivable but fragile in PromQL |
| **Priority** | 🟡 Medium |

### 4.4 `greenkube_node_cpu_allocated_millicores`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `node`, `instance_type`, `zone`, `region`, `cloud_provider`, `architecture` |
| **Description** | Sum of `cpu_request_millicores` of all pods scheduled on the node. Expresses infrastructure allocation pressure. |
| **Status** | ❌ Not currently computed or exposed. Requires aggregation over pod metrics grouped by node. |
| **Priority** | 🔴 High |

### 4.5 `greenkube_node_memory_allocated_bytes`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `node`, `instance_type`, `zone`, `region`, `cloud_provider`, `architecture` |
| **Description** | Sum of `memory_request_bytes` of all pods scheduled on the node. |
| **Status** | ❌ Not currently computed or exposed. |
| **Priority** | 🔴 High |

### 4.6 `greenkube_pod_storage_request_bytes`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `cluster`, `namespace`, `pod`, `node`, `region` |
| **Description** | Total PVC storage requested by the pod in bytes |
| **Status** | `storage_request_bytes` field exists in `CombinedMetric` model but is never populated nor exposed |
| **Priority** | 🟢 Low (depends on PVC collector maturity) |

### 4.7 `greenkube_pod_storage_usage_bytes`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `cluster`, `namespace`, `pod`, `node`, `region` |
| **Description** | Actual PVC storage usage in bytes |
| **Status** | `storage_usage_bytes` field exists in `CombinedMetric` model but is never populated nor exposed |
| **Priority** | 🟢 Low |

### 4.8 `greenkube_namespace_recommendation_savings_co2e_grams_total`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `cluster`, `namespace` |
| **Description** | Total potential CO₂e savings (in grams) from all active recommendations targeting this namespace |
| **Status** | ❌ Not currently computed or exposed. Recommendations currently carry no `namespace` label, only `type` and `priority`. Requires adding a `namespace` label to the recommendation model and aggregating savings per namespace in `update_recommendation_metrics()`. |
| **Priority** | 🔴 High — required by Command Center panels 9, 10, 12 |

### 4.9 `greenkube_namespace_recommendation_savings_cost_dollars_total`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `cluster`, `namespace` |
| **Description** | Total potential cost savings (in dollars) from all active recommendations targeting this namespace |
| **Status** | ❌ Same as 4.8 — requires `namespace` label on recommendations |
| **Priority** | 🔴 High |

### 4.10 `greenkube_cluster_co2e_saved_grams_total`

| Attribute | Value |
|-----------|-------|
| **Type** | Counter (exposed as Gauge) |
| **Labels** | `cluster` |
| **Description** | Cumulative CO₂e emissions (in grams) avoided since GreenKube was installed, attributed to implemented recommendations. Calculated as the delta between the CO₂e of the pod before a recommendation was applied and after. |
| **Status** | ❌ Requires a new **Realized Savings** tracking subsystem — see §5, Task 1.4 for design. This is the highest-priority new product feature. |
| **Priority** | 🔴 High |

### 4.11 `greenkube_cluster_cost_saved_dollars_total`

| Attribute | Value |
|-----------|-------|
| **Type** | Counter (exposed as Gauge) |
| **Labels** | `cluster` |
| **Description** | Cumulative cloud cost (in dollars) saved since GreenKube was installed, attributed to implemented recommendations. |
| **Status** | ❌ Same as 4.10 — requires the Realized Savings tracking subsystem. |
| **Priority** | 🔴 High |

### 4.12 `greenkube_recommendations_implemented_total`

| Attribute | Value |
|-----------|-------|
| **Type** | Gauge |
| **Labels** | `cluster`, `type` |
| **Description** | Number of recommendations that have been automatically detected as implemented (i.e., the suggested change has been observed in the cluster). |
| **Status** | ❌ Requires automatic reconciliation logic — see §5, Task 1.4. |
| **Priority** | 🔴 High |

---

## 5. Implementation Plan

### Phase 1 — High-Impact / Low-Effort (Sprint 1)

**Goal:** Expose already-collected data and compute node allocation metrics.

#### Task 1.1 — Expose `greenkube_pod_restart_count`

- **File:** `src/greenkube/api/metrics_endpoint.py`
- **Action:** Add a new `Gauge` `POD_RESTART_COUNT` with `POD_LABELS`. In `update_cluster_metrics()`, set the value from `m.restart_count` (already in `CombinedMetric`).
- **Test:** `tests/api/test_metrics_endpoint.py` — add assertion that restart_count gauge is set.
- **Effort:** ~30 min

#### Task 1.2 — Expose `greenkube_node_cpu_allocated_millicores` and `greenkube_node_memory_allocated_bytes`

- **File:** `src/greenkube/api/metrics_endpoint.py`
- **Action:**
  1. Add two new `Gauge`s with `NODE_LABELS`.
  2. In `update_cluster_metrics()`, aggregate `cpu_request` and `memory_request` grouped by `m.node` to build per-node allocation sums.
  3. In `update_node_metrics()`, set the allocation gauges using the per-node maps. Since `update_cluster_metrics` runs first in `refresh_metrics_from_db`, pass the aggregated maps between the two functions (or compute inline).
- **Architecture note:** The aggregation is pure computation over already-available `CombinedMetric` data — no new collectors or DB queries needed.
- **Test:** `tests/api/test_metrics_endpoint.py` — add assertions.
- **Effort:** ~1h

#### Task 1.3 — Add `namespace` label to recommendations and expose per-namespace savings

- **Files:**
  - `src/greenkube/models/metrics.py` — add `namespace: Optional[str]` field to `Recommendation`.
  - `src/greenkube/core/` (engine / estimator) — ensure namespace is propagated when recommendations are generated.
  - `src/greenkube/api/metrics_endpoint.py` — add `NS_RECOMMENDATION_SAVINGS_CO2` and `NS_RECOMMENDATION_SAVINGS_COST` gauges with labels `["cluster", "namespace"]`; aggregate in `update_recommendation_metrics()`.
- **Architecture note:** The `Recommendation` model must carry `namespace` because recommendations are always scoped to a specific pod/namespace (e.g. "right-size pod X in namespace Y"). This is a data model change — verify storage layer migration is not needed or add a DB migration.
- **Test:** `tests/api/test_metrics_endpoint.py` — add assertion for per-namespace savings gauges.
- **Effort:** ~2h

#### Task 1.4 — Realized Savings tracking subsystem

This is the most significant new product feature. It enables the three "GreenKube impact" metrics in the Command Center (§4.10–4.12).

**Design:**

The core idea is **automatic reconciliation**: after a recommendation is issued for pod `P` in namespace `N`, GreenKube monitors `P`'s resource profile. If a subsequent collection shows that `P`'s CPU/memory requests have decreased in the direction suggested, the recommendation is marked `implemented` and the CO₂e/cost delta is attributed as realized savings.

```
Realized CO₂e saving = co2e_before_collection - co2e_after_collection
(attributed when: recommendation was active AND resource profile changed toward suggestion)
```

**New components:**

1. **`RecommendationReconciler`** (`src/greenkube/core/recommendation_reconciler.py`)
   - Runs after each collection cycle.
   - For each `open` recommendation, compares current pod metrics to the snapshot taken when the recommendation was issued.
   - If the suggested change is detected (e.g. CPU request reduced, zombie pod deleted), marks it `implemented`, records `realized_co2e_grams` and `realized_cost_dollars`.
   - If the pod no longer exists (deleted zombie), also marks it `implemented`.

2. **DB schema additions** (`src/greenkube/storage/`)
   - `recommendations` table: add columns `status` (`open` | `implemented` | `expired`), `implemented_at`, `realized_co2e_grams`, `realized_cost_dollars`.
   - DB migration required.

3. **Prometheus gauges** (`src/greenkube/api/metrics_endpoint.py`)
   - `CLUSTER_CO2_SAVED` — sum of `realized_co2e_grams` across all `implemented` recommendations in the cluster.
   - `CLUSTER_COST_SAVED` — sum of `realized_cost_dollars`.
   - `RECOMMENDATIONS_IMPLEMENTED` — count of `implemented` recommendations by `type`.

4. **`update_recommendation_metrics()` extension**
   - Accept `implemented_recommendations` list in addition to `open` ones.
   - Set the three new gauges.

- **Files:** `src/greenkube/core/recommendation_reconciler.py` (new), `src/greenkube/storage/` (migration), `src/greenkube/api/metrics_endpoint.py`, `src/greenkube/models/metrics.py`.
- **Test:** `tests/core/test_recommendation_reconciler.py` (new).
- **Effort:** ~1 day (reconciler) + ~2h (DB migration + gauges).

### Phase 2 — Medium-Impact / Low-Effort (Sprint 1–2)

**Goal:** Add derived efficiency ratios as explicit metrics to simplify dashboard queries and enable native Prometheus alerting.

#### Task 2.1 — Expose `greenkube_pod_cpu_efficiency_ratio` and `greenkube_pod_memory_efficiency_ratio`

- **File:** `src/greenkube/api/metrics_endpoint.py`
- **Action:** After setting `POD_CPU_REQUEST` and `POD_CPU_USAGE`, compute `ratio = usage / request` (skip if request == 0, cap at 1.0) and set the new gauge.
- **Effort:** ~30 min

### Phase 3 — Storage Metrics (Sprint 3+)

**Goal:** Complete the storage visibility picture.

#### Task 3.1 — Populate `storage_request_bytes` and `storage_usage_bytes` in `CombinedMetric`

- **Investigation needed:** Confirm whether the Kubernetes collector already fetches PVC data or if a new query is needed.
- **Files:** `src/greenkube/collectors/` (K8s collector), `src/greenkube/api/metrics_endpoint.py`.
- **Effort:** ~1 day (depends on PVC collection maturity)

### Phase 4 — Dashboard Update (after Phase 1 & 2)

**Goal:** Rebuild `dashboards/greenkube-grafana.json` to match this specification exactly.

- Implement all panels defined in §3.
- Remove panels that exist today but do not appear in this spec (they can be moved to a "detailed" secondary dashboard).
- Update `docs/grafana.md` with the new panel inventory.
- Publish dashboard to Grafana.com dashboard catalogue (ArtifactHub-compatible).

---

## 6. Panels to Remove from Current Dashboard

The following panel types exist in the current JSON but should be removed from the main dashboard (they reduce signal-to-noise):

| Panel | Reason |
|-------|--------|
| Raw CPU/Memory time series per pod (ungrouped) | Too granular for top-level — move to a dedicated "Pod Deep Dive" dashboard |
| State-timeline panels | Confusing for new users, low information density |
| Redundant KPI duplicates | Current dashboard has near-duplicate stat rows |

These panels can live in a secondary **GreenKube — Pod Deep Dive** dashboard, linked from the main one.

---

## 7. Alerting Rules (Recommended)

As part of the dashboard, ship a companion `PrometheusRule` (Helm-managed) with the following alerts:

| Alert | Expression | Severity |
|-------|------------|----------|
| `GreenKubeDataStale` | `time() - greenkube_last_collection_timestamp_seconds > 600` | warning |
| `GreenKubeSustainabilityScoreLow` | `greenkube_sustainability_score < 40` | warning |
| `GreenKubeHighCarbonIntensity` | `greenkube_carbon_intensity_score > 400` | info |
| `GreenKubeHighEstimationRatio` | `greenkube_estimated_metrics_ratio > 0.5` | warning |
| `GreenKubeHighCPUWaste` | `sum(greenkube_pod_cpu_efficiency_ratio < 0.2) / count(greenkube_pod_cpu_efficiency_ratio) > 0.3` | warning |

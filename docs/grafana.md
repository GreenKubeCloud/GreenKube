# Prometheus & Grafana Integration

GreenKube has two distinct Prometheus integrations that serve different purposes.

## How they relate

| Direction | What it does | Required? |
|---|---|---|
| GreenKube → Prometheus | GreenKube queries your cluster's Prometheus for CPU, memory, network, and disk metrics | Strongly recommended |
| Prometheus → GreenKube | Prometheus scrapes GreenKube's `/prometheus/metrics` endpoint to expose CO₂e, cost, and sustainability metrics in Grafana | Optional |

---

## 1. Scraping GreenKube metrics into Prometheus

### With the Prometheus Operator (kube-prometheus-stack)

Enable the `ServiceMonitor` in `values.yaml`:

```yaml
monitoring:
  serviceMonitor:
    enabled: true           # Creates a ServiceMonitor CRD resource
    namespace: monitoring   # Namespace where Prometheus looks for ServiceMonitors
    interval: 30s           # Scrape interval (30s recommended)
  networkPolicy:
    enabled: true           # Opens the Prometheus → greenkube-api network path
    prometheusNamespace: monitoring
```

Or via `--set`:

```bash
helm upgrade greenkube greenkube/greenkube \
  -n greenkube \
  --set monitoring.serviceMonitor.enabled=true \
  --set monitoring.networkPolicy.enabled=true
```

The `ServiceMonitor` scrapes the `http` port (8000) at `/prometheus/metrics` every 30 seconds.
It also restores custom metric labels (`cluster`, `region`, `zone`, `namespace`) that
Prometheus would otherwise overwrite with the scrape-target labels.

### Without the Prometheus Operator

Add a static scrape job to `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: greenkube
    scrape_interval: 30s
    metrics_path: /prometheus/metrics
    static_configs:
      - targets:
          - greenkube-api.greenkube.svc.cluster.local:8000
```

---

## 2. Cluster name auto-detection

GreenKube reads `K8S_NODE_NAME` (injected via the Kubernetes Downward API) and resolves the
cluster name in this order:

1. `CLUSTER_NAME` env var (if explicitly set)
2. `K8S_NODE_NAME` → query node labels for EKS / GKE / AKS provider-specific cluster tag
3. `K8S_NODE_NAME` itself (works on minikube and bare-metal)
4. `"default"` (final fallback)

All emitted metrics carry a `cluster` label set to this resolved name.

---

## 3. Available Prometheus metrics

Every metric is a Prometheus **Gauge** (point-in-time snapshot, refreshed on each scrape).

### Cluster-wide metrics

| Metric | Labels | Description |
|---|---|---|
| `greenkube_sustainability_score` | `cluster` | Composite sustainability score (0–100) |
| `greenkube_cluster_co2e_grams_total` | `cluster` | Total operational CO₂e emissions (g) |
| `greenkube_cluster_embodied_co2e_grams_total` | `cluster` | Total embodied (Scope 3) CO₂e (g) |
| `greenkube_cluster_cost_dollars_total` | `cluster` | Estimated total cloud cost ($) |
| `greenkube_cluster_energy_joules_total` | `cluster` | Total energy consumed (J) |
| `greenkube_cluster_pod_count` | `cluster` | Number of running pods |
| `greenkube_cluster_namespace_count` | `cluster` | Number of active namespaces |
| `greenkube_cluster_co2e_saved_grams_total` | `cluster` | Annual projected CO₂e savings from open recommendations (g) |
| `greenkube_cluster_cost_saved_dollars_total` | `cluster` | Annual projected cost savings from open recommendations ($) |
| `greenkube_pue` | `cluster`, `namespace`, `node`, `region` | Power Usage Effectiveness per node |
| `greenkube_carbon_intensity_zone` | `cluster`, `namespace`, `zone` | Current grid carbon intensity by zone (gCO₂/kWh) |
| `greenkube_grid_intensity_gco2_kwh` | `cluster`, `namespace`, `node`, `region` | Grid carbon intensity per node region |
| `greenkube_carbon_intensity_score` | `cluster` | Carbon intensity score component (0–100) |
| `greenkube_sustainability_dimension_score` | `cluster`, `dimension` | Score per sustainability dimension |
| `greenkube_estimated_metrics_ratio` | `cluster` | Fraction of metrics that are estimated (0 = all measured) |
| `greenkube_metrics_total` | — | Total number of metrics collected in the last window |
| `greenkube_last_collection_timestamp_seconds` | — | Unix timestamp of the last successful collection |

### Namespace-level metrics

| Metric | Labels | Description |
|---|---|---|
| `greenkube_namespace_co2e_grams_total` | `cluster`, `namespace` | Operational CO₂e per namespace (g) |
| `greenkube_namespace_embodied_co2e_grams_total` | `cluster`, `namespace` | Embodied CO₂e per namespace (g) |
| `greenkube_namespace_cost_dollars_total` | `cluster`, `namespace` | Cloud cost per namespace ($) |
| `greenkube_namespace_energy_joules_total` | `cluster`, `namespace` | Energy per namespace (J) |
| `greenkube_namespace_pod_count` | `cluster`, `namespace` | Pod count per namespace |
| `greenkube_namespace_recommendation_savings_co2e_grams_total` | `cluster`, `namespace` | Projected annual CO₂e savings for namespace (g) |
| `greenkube_namespace_recommendation_savings_cost_dollars_total` | `cluster`, `namespace` | Projected annual cost savings for namespace ($) |

### Pod-level metrics

| Metric | Labels | Description |
|---|---|---|
| `greenkube_pod_co2e_grams` | `cluster`, `namespace`, `node`, `region` | CO₂e per pod (g) |
| `greenkube_pod_embodied_co2e_grams` | `cluster`, `namespace`, `node`, `region` | Embodied CO₂e per pod (g) |
| `greenkube_pod_cost_dollars` | `cluster`, `namespace`, `node`, `region` | Cost per pod ($) |
| `greenkube_pod_energy_joules` | `cluster`, `namespace`, `node`, `region` | Energy per pod (J) |
| `greenkube_pod_cpu_usage_millicores` | `cluster`, `namespace`, `node`, `region` | CPU usage (millicores) |
| `greenkube_pod_cpu_request_millicores` | `cluster`, `namespace`, `node`, `region` | CPU request (millicores) |
| `greenkube_pod_cpu_efficiency_ratio` | `cluster`, `namespace`, `node`, `region` | CPU efficiency (usage ÷ request) |
| `greenkube_pod_memory_usage_bytes` | `cluster`, `namespace`, `node`, `region` | Memory usage (bytes) |
| `greenkube_pod_memory_request_bytes` | `cluster`, `namespace`, `node`, `region` | Memory request (bytes) |
| `greenkube_pod_memory_efficiency_ratio` | `cluster`, `namespace`, `node`, `region` | Memory efficiency (usage ÷ request) |
| `greenkube_pod_network_receive_bytes` | `cluster`, `namespace`, `node`, `region` | Network RX (bytes) |
| `greenkube_pod_network_transmit_bytes` | `cluster`, `namespace`, `node`, `region` | Network TX (bytes) |
| `greenkube_pod_disk_read_bytes` | `cluster`, `namespace`, `node`, `region` | Disk read (bytes) |
| `greenkube_pod_disk_write_bytes` | `cluster`, `namespace`, `node`, `region` | Disk write (bytes) |
| `greenkube_pod_restart_count` | `cluster`, `namespace`, `node`, `region` | Pod restart count |

### Node metrics

| Metric | Labels | Description |
|---|---|---|
| `greenkube_node_info` | `cluster`, `node`, `region`, `zone`, `architecture`, `cloud_provider`, `instance_type` | Node metadata gauge (always 1) |
| `greenkube_node_cpu_capacity_millicores` | `node` | Node total CPU capacity (millicores) |
| `greenkube_node_cpu_allocated_millicores` | `node` | CPU allocated to pods (millicores) |
| `greenkube_node_memory_capacity_bytes` | `node` | Node total memory capacity (bytes) |
| `greenkube_node_memory_allocated_bytes` | `node` | Memory allocated to pods (bytes) |
| `greenkube_node_embodied_emissions_kg` | `node` | Node embodied CO₂e (kg) |

### Recommendations & savings metrics

| Metric | Labels | Description |
|---|---|---|
| `greenkube_recommendations_total` | `cluster`, `type`, `priority` | Count of active open recommendations |
| `greenkube_recommendations_implemented_total` | `cluster`, `type` | Count of implemented recommendations |
| `greenkube_recommendations_savings_co2e_grams` | `cluster`, `type` | Projected annual CO₂e savings per recommendation type (g) |
| `greenkube_recommendations_savings_cost_dollars` | `cluster`, `type` | Projected annual cost savings per recommendation type ($) |
| `greenkube_co2e_savings_attributed_grams_total` | `cluster`, `recommendation_type` | **Realized** CO₂e savings — prorated ledger (cumulative g) |
| `greenkube_cost_savings_attributed_dollars_total` | `cluster`, `recommendation_type` | **Realized** cost savings — prorated ledger (cumulative $) |

> **Realized vs. projected savings**
>
> `greenkube_co2e_savings_attributed_grams_total` and `greenkube_cost_savings_attributed_dollars_total`
> are written to a DB ledger by the scheduler every collection period. They represent *prorated* savings
> for each period a recommendation was active. Use `increase(metric[$__range])` in Grafana to sum
> savings over any selected window.
>
> The `_saved_grams_total` / `_saved_dollars_total` cluster metrics are *annual projections* only.

---

## 4. Grafana dashboard

A pre-built dashboard is available at `dashboards/greenkube-grafana.json`.

### Import

> ⚠️ Always use `/api/dashboards/import` (not `/api/dashboards/db`). Only the import endpoint
> resolves the `${DS_PROMETHEUS}` variable; using `db` leaves all panels with "datasource is not set".

**Via UI:** Dashboards → Import → Upload JSON file → select your Prometheus datasource.

**Via script:**

```python
import json, urllib.request, base64

with open("dashboards/greenkube-grafana.json") as f:
    dashboard = json.load(f)

payload = json.dumps({
    "dashboard": dashboard,
    "folderId": 0,
    "overwrite": True,
    "inputs": [{
        "name": "DS_PROMETHEUS",
        "type": "datasource",
        "pluginId": "prometheus",
        "value": "<YOUR_PROMETHEUS_DATASOURCE_UID>",
    }]
}).encode()

req = urllib.request.Request(
    "http://<GRAFANA_HOST>/api/dashboards/import",
    data=payload,
    headers={
        "Content-Type": "application/json",
        "Authorization": "Basic " + base64.b64encode(b"admin:password").decode(),
    }
)
with urllib.request.urlopen(req) as r:
    print(json.load(r).get("importedUrl"))
```

Find your Prometheus datasource UID in Grafana → Connections → Data sources → select your
Prometheus instance → copy the UID from the URL.

### Template variables

| Variable | Query | Description |
|---|---|---|
| `DS_PROMETHEUS` | datasource plugin | Grafana Prometheus datasource |
| `cluster` | `label_values(greenkube_cluster_co2e_grams_total, cluster)` | Filter by cluster name |
| `namespace` | `label_values(greenkube_namespace_co2e_grams_total{cluster="$cluster"}, namespace)` | Filter by namespace |
| `node` | `label_values(greenkube_node_info, node)` | Filter by node |
| `region` | `label_values(greenkube_pod_co2e_grams, region)` | Filter by region |

### Dashboard rows

| Row | Key panels | Notes |
|---|---|---|
| **Sustainability Command Center** | Score gauge, CO₂e / Cost / Pods stats, CO₂e & Cost Avoided, Active Recommendations, Top-3 namespace bars | Always-visible summary |
| **Carbon, Cost & Energy Trends** | CO₂e / Cost / Energy over time, Grid intensity by zone | Timeseries — full dashboard time range |
| **Sustainability Score Breakdown** | Score by dimension bar gauge, Score over time, Data quality gauge | |
| **Resource Efficiency** | CPU/Memory efficiency by namespace, worst-efficiency pods | |
| **Namespace Analysis** | CO₂e / Cost / Energy pie charts, Namespace summary table | |
| **Top Emitters & Spenders** | Top 15 pods by CO₂e, cost, energy, embodied | |
| **Node Analysis** | CPU/Memory allocation ratios, embodied emissions, Node inventory | |
| **Network & Storage I/O** | Top 10 pods — RX / TX / disk read / disk write | |
| **Pod Stability** | Top restarting pods, restarts by namespace | |
| **Recommendations & Savings** | Active recommendations, CO₂e & Cost Avoided, by-type breakdown | |
| **GreenKube Self-Monitoring** | Last collection age, metrics count, estimated ratio, PUE, carbon intensity score | |

### Grafana 12 compatibility

GreenKube's dashboard targets **Grafana 12**. Several non-obvious choices were required:

| Issue | Root cause | Solution |
|---|---|---|
| `instant: true` returns empty frames | Grafana 12 changed how the Prometheus proxy handles instant queries | All targets use `"range": true, "instant": false` |
| Snapshot panels show "No data" when dashboard time range > available history | Range queries over months return no data when GreenKube has only been running for hours | Snapshot panels (stat / gauge / bargauge / piechart / table) have `"timeFrom": "5m"` — they always query the last 5 minutes regardless of the dashboard time-range selector |
| `increase()` panels must accumulate over the full selected window | `timeFrom: "5m"` would collapse the window | `CO₂e Avoided` and `Cost Avoided` panels intentionally have **no** `timeFrom` override |
| Cluster-level timeseries show multiple overlapping lines after pod rollouts | Old pod instances remain in Prometheus TSDB during the scrape staleness window | Cluster timeseries use `max by (cluster)(...)` to deduplicate |
| `${DS_PROMETHEUS}` not resolved | `/api/dashboards/db` skips template-variable resolution | Import via `/api/dashboards/import` with `inputs` array |

### Rebuilding the dashboard JSON

```bash
# Regenerate JSON
python scripts/build_grafana_dashboard.py

# Reimport (replace <UID> and credentials)
python - <<'EOF'
import json, urllib.request, base64
with open("dashboards/greenkube-grafana.json") as f:
    dashboard = json.load(f)
payload = json.dumps({
    "dashboard": dashboard, "folderId": 0, "overwrite": True,
    "inputs": [{"name": "DS_PROMETHEUS", "type": "datasource",
                "pluginId": "prometheus", "value": "<PROMETHEUS_DATASOURCE_UID>"}]
}).encode()
req = urllib.request.Request("http://<GRAFANA_HOST>/api/dashboards/import",
    data=payload,
    headers={"Content-Type": "application/json",
             "Authorization": "Basic " + base64.b64encode(b"admin:password").decode()})
with urllib.request.urlopen(req) as r:
    print(json.load(r).get("importedUrl"))
EOF
```

---

## 5. Troubleshooting

### "No data" in all panels

1. **ServiceMonitor not enabled** — in the Prometheus UI (`/targets`), verify `greenkube-api` is `UP`.
2. **Missing `cluster` label** — run `greenkube_sustainability_score` in Prometheus Explore. If the result has no `cluster` label, verify that `K8S_NODE_NAME` is injected via the Downward API in the Helm deployment (`helm-chart/templates/deployment.yaml`).
3. **`DS_PROMETHEUS` not resolved** — open dashboard settings → Variables. `DS_PROMETHEUS` must show your Prometheus datasource name, not the literal string `${DS_PROMETHEUS}`. Re-import using `/api/dashboards/import`.
4. **GreenKube just started** — metrics are populated after the first scheduler run (≤ 5 minutes). Stat panels show `0` until then; they self-correct once the scheduler completes its first cycle.

### Datasource shows "not set"

The dashboard was imported via `/api/dashboards/db`. Re-import using the Python script above.

### Trend panels show "No data" with a large time range

Timeseries panels show data only for the period GreenKube has been running.
Snapshot panels (stat / gauge / bargauge / piechart / table) are unaffected — they always
display the current value via the `"timeFrom": "5m"` panel override.

### Recommendations panels show "No data" when a specific namespace is selected

`greenkube_recommendations_total` carries `namespace=greenkube` (the GreenKube pod's own
Kubernetes namespace, not a monitored application namespace). Set the **Namespace** variable
to **All** to include recommendations in filtered views.


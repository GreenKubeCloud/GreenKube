# Prometheus & Grafana Integration

GreenKube has two distinct Prometheus integrations that serve different purposes.

## How they relate

| Direction | What it does | Required? |
|-----------|-------------|-----------|
| GreenKube → Prometheus | GreenKube queries your cluster's Prometheus for CPU, memory, network, and disk metrics | Strongly recommended — no resource metrics without it |
| Prometheus → GreenKube | Prometheus scrapes GreenKube's `/prometheus/metrics` endpoint to expose CO₂e, cost, and energy metrics in Grafana | Optional |

The first integration is automatic. The second requires a small amount of configuration.

## Exposing GreenKube metrics to Prometheus

### With the Prometheus Operator (kube-prometheus-stack)

Enable the `ServiceMonitor` in your `values.yaml`:

```yaml
monitoring:
  serviceMonitor:
    enabled: true          # Creates a ServiceMonitor resource
    namespace: monitoring  # Must match your Prometheus serviceMonitorNamespaceSelector
    interval: 30s
  networkPolicy:
    enabled: true          # Allows Prometheus to reach the GreenKube API port
    prometheusNamespace: monitoring
```

Or via `--set`:

```bash
helm upgrade greenkube greenkube/greenkube \
  -n greenkube \
  --set monitoring.serviceMonitor.enabled=true \
  --set monitoring.networkPolicy.enabled=true
```

### Without the Prometheus Operator

Add a static scrape target to your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: greenkube
    scrape_interval: 30s
    metrics_path: /prometheus/metrics
    static_configs:
      - targets:
          - greenkube-api.greenkube.svc.cluster.local:8000
```

## Available Prometheus metrics

See the [API reference](api.md#prometheus-metrics-endpoint) for the full list of exposed metrics.

## Grafana dashboard

A pre-built dashboard is available at `dashboards/greenkube-grafana.json`.

**To import:**

1. In Grafana, go to **Dashboards → Import**.
2. Upload `dashboards/greenkube-grafana.json` or paste its contents.
3. Select your Prometheus data source.
4. Click **Import**.

**Dashboard contents:**

- KPI row: total CO₂e, cost, energy, active pods and nodes
- Time-series: CO₂e, cost, and energy over time
- Namespace breakdown: pie charts for CO₂e and cost
- Top pods: heaviest emitters and most expensive pods
- Node utilisation: CPU and memory usage per node
- Grid intensity: carbon intensity over time per zone
- Sustainability score: composite 0–100 gauge, per-dimension breakdown, and score over time — filterable by `cluster` and `region`
- Recommendations: summary table of open optimization suggestions

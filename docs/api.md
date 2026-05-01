# API Reference

The GreenKube REST API is available at `/api/v1`. Interactive documentation (Swagger UI) is served at `/api/v1/docs`.

## Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/v1/health` | Health check and version |
| `GET` | `/api/v1/version` | Application version |
| `GET` | `/api/v1/config` | Current runtime configuration |
| `GET` | `/api/v1/metrics` | Per-pod metrics (`?namespace=&last=24h`) |
| `GET` | `/api/v1/metrics/summary` | Aggregated cluster summary (`?namespace=&last=24h`) |
| `GET` | `/api/v1/metrics/timeseries` | Time-series data (`?granularity=day&last=7d`) |
| `GET` | `/api/v1/namespaces` | List of active namespaces |
| `GET` | `/api/v1/nodes` | Cluster node inventory |
| `GET` | `/api/v1/recommendations` | Optimization recommendations (`?namespace=`) |
| `GET` | `/api/v1/report/summary` | Report preview — row count and totals (`?namespace=&last=24h&aggregate=true&granularity=daily`) |
| `GET` | `/api/v1/report/export` | Download report as CSV or JSON (`?format=csv&last=7d&aggregate=true&granularity=daily`) |

## Query Parameters

### Time range (`last`)

All metric and report endpoints accept a `last` parameter to define the time window:

| Value | Description |
|-------|-------------|
| `1h`, `6h`, `24h` | Last N hours |
| `7d`, `30d`, `90d` | Last N days |
| `3m`, `6m`, `12m` | Last N months |
| `ytd` | Year to date, from Jan 1 UTC through now |

### Granularity

Used in timeseries and report endpoints:

| Value | Description |
|-------|-------------|
| `hour` | Hourly buckets |
| `day` | Daily buckets (default) |
| `week` | Weekly buckets |
| `month` | Monthly buckets |
| `year` | Yearly buckets |

## Examples

```bash
# Health check
curl http://localhost:8000/api/v1/health
# {"status":"ok","version":"0.2.9"}

# Per-pod metrics for the last 24 hours
curl "http://localhost:8000/api/v1/metrics?last=24h"

# Aggregated summary for a specific namespace over the last 7 days
curl "http://localhost:8000/api/v1/metrics/summary?namespace=production&last=7d"

# Hourly time-series data for the last 7 days
curl "http://localhost:8000/api/v1/metrics/timeseries?granularity=hour&last=7d"

# Optimization recommendations for a namespace
curl "http://localhost:8000/api/v1/recommendations?namespace=production"

# Preview a report before downloading
curl "http://localhost:8000/api/v1/report/summary?last=ytd&aggregate=true&granularity=monthly"

# Download a daily CSV report for the last 7 days
curl -O -J "http://localhost:8000/api/v1/report/export?format=csv&last=7d&aggregate=true&granularity=daily"

# Download a raw JSON report for a namespace
curl -O -J "http://localhost:8000/api/v1/report/export?format=json&last=30d&namespace=production"
```

## Prometheus Metrics Endpoint

GreenKube exposes its own computed metrics for scraping by Prometheus at:

```
GET /prometheus/metrics
```

Available metrics:

| Metric | Labels | Description |
|--------|--------|-------------|
| `greenkube_pod_co2e_grams` | `cluster`, `namespace`, `pod`, `node`, `region` | CO₂e emissions per pod |
| `greenkube_pod_energy_joules` | `cluster`, `namespace`, `pod` | Energy consumption per pod |
| `greenkube_pod_cost_dollars` | `cluster`, `namespace`, `pod` | Cost per pod |
| `greenkube_pod_cpu_usage_millicores` | `cluster`, `namespace`, `pod` | CPU usage |
| `greenkube_pod_memory_usage_bytes` | `cluster`, `namespace`, `pod` | Memory usage |
| `greenkube_pod_network_receive_bytes` | `cluster`, `namespace`, `pod` | Network received |
| `greenkube_pod_network_transmit_bytes` | `cluster`, `namespace`, `pod` | Network transmitted |
| `greenkube_sustainability_score` | `cluster` | Composite sustainability score (0–100) |
| `greenkube_sustainability_dimension_score` | `cluster`, `dimension` | Per-dimension sustainability score |
| `greenkube_carbon_intensity_score` | — | Energy-weighted average grid carbon intensity (gCO₂e/kWh) |
| `greenkube_carbon_intensity_zone` | `zone` | Real-time grid carbon intensity per zone |
| `greenkube_recommendation_total` | `type` | Recommendation counts by type |
| `greenkube_node_info` | `instance_type`, `zone`, `capacity` | Node metadata |

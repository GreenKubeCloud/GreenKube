# <img src="https://raw.githubusercontent.com/GreenKubeCloud/GreenKube/refs/heads/gh-pages/assets/greenkube-logo.png" alt="GreenKube Logo" style="height: 80px; vertical-align: middle;"> **GreenKube**

**Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure. Make your cloud operations both cost-effective and environmentally responsible.**

GreenKube is an open-source tool designed to help DevOps, SRE, and FinOps teams navigate the complexity of sustainability reporting (CSRD) and optimize their cloud costs (FinOps) through better energy efficiency (GreenOps).

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![GitHub Stars](https://img.shields.io/github/stars/GreenKubeCloud/greenkube?style=social)](https://github.com/GreenKubeCloud/greenkube/stargazers)
[![Build in Public](https://img.shields.io/badge/Build%20in-Public-blueviolet)](https://github.com/GreenKubeCloud/greenkube)


## 🎯 Mission

The EU's Corporate Sustainability Reporting Directive (CSRD) requires companies to report the carbon footprint of their value chain—including cloud services (Scope 3). GreenKube addresses this urgent need by providing tools to:

1.  **Estimate** the energy consumption and CO₂e emissions of each Kubernetes workload.
2.  **Report** these metrics in a format aligned with regulatory requirements (ESRS E1).
3.  **Optimize** infrastructure to simultaneously reduce cloud bills and environmental impact.

## ✨ Features (Version 0.1.7)

* **Web Dashboard:** Built-in SvelteKit SPA with real-time charts (ECharts), per-pod metrics table, node inventory, and optimization recommendations — all served from the same container as the API.
* **REST API:** Full-featured FastAPI backend with endpoints for metrics, nodes, namespaces, recommendations, timeseries, and configuration. OpenAPI docs included.
* **Prometheus-Based Energy Estimation:** Calculates pod-level energy consumption (Joules) using CPU usage data from Prometheus and a built-in library of instance power profiles.
* **Optimization Recommendations:** Identifies "zombie" pods (idle but costly) and "oversized" pods (underutilized CPU) to help you rightsize and reduce waste.
* **Pod & Namespace Reporting:** Generates detailed reports of CO₂e emissions, energy usage, and (optional) costs per pod and namespace.
* **Flexible Data Backends:** Supports PostgreSQL (default), SQLite, and Elasticsearch for storing and querying historical carbon intensity data.
* **Historical Analysis:** Report on energy and carbon usage over any time period (`--last 7d`, `--last 3m`) with flexible grouping (`--daily`, `--monthly`, etc.).
* **Service Auto-Discovery:** Automatically discovers in-cluster Prometheus and OpenCost services to simplify setup (can be manually overridden).
* **Helm Chart Deployment:** Easily deploy and configure GreenKube in any Kubernetes cluster via a public Helm repository.
* **Data Export:** Export reports to CSV or JSON for integration with other tools.


## 📦 Dependencies

The chart requires the following services to be available in the cluster:

- **OpenCost** – for cost data.
- **Prometheus** – for metrics collection.

GreenKube uses service auto‑discovery to locate these services automatically. If they are deployed in non‑standard namespaces or with custom names, auto‑discovery may fail. In that case, set the service URLs manually in `values.yaml` (see the `prometheus.url` and `opencost.url` fields).

## 🚀 Installation & Usage

The recommended way to install GreenKube is via the official Helm chart.

### 1. Add the GreenKube Helm Repository

First, add the GreenKube chart repository to your local Helm setup:

```bash
helm repo add greenkube https://GreenKubeCloud.github.io/GreenKube
helm repo update
```

### 2. Configure Your Installation

Create a file named `my-values.yaml` to customize your deployment:

```yaml
secrets:
  # Get your API token from https://www.electricitymaps.com/
  # Optional: without it, GreenKube uses a default carbon intensity
  # value (configurable via config.defaultIntensity) for all zones.
  electricityMapsToken: "YOUR_API_TOKEN_HERE"

# Uncomment to manually set your Prometheus URL
# (If left empty, GreenKube will try to auto-discover it)
# config:
#   prometheus:
#     url: "http://prometheus-k8s.monitoring.svc.cluster.local:9090"
```

> **Note:** GreenKube works without an Electricity Maps token. When no token is provided, a default carbon intensity value (`config.defaultIntensity`, default: 500 gCO₂e/kWh) is used for all zones. This gives approximate results. For accurate, zone-specific carbon data, provide a token from [Electricity Maps](https://www.electricitymaps.com/).

#### Install the Chart

Install the Helm chart into a dedicated namespace (e.g., `greenkube`):

```bash
helm install greenkube greenkube/greenkube \
  -f my-values.yaml \
  -n greenkube \
  --create-namespace
```

This deploys GreenKube with the collector, the API server, and the web dashboard — all in a single image.

## 🖥️ Web Dashboard

GreenKube ships with a built-in web dashboard (SvelteKit SPA served by the API). Once deployed, access it via port-forward:

```bash
kubectl port-forward svc/greenkube-api 8000:8000 -n greenkube
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

The dashboard includes:
- **Dashboard** — KPI cards (CO₂, cost, energy, pods), time-series charts, namespace breakdown, top pods
- **Metrics** — Sortable and searchable per-pod metrics table with energy and cost charts
- **Nodes** — Cluster node inventory with CPU/memory capacity bars and hardware profiles
- **Recommendations** — Actionable suggestions (zombie pods, CPU rightsizing) with potential savings
- **Settings** — Current configuration, API health status, and version info

## 🔌 API Reference

The API is available at `/api/v1` and serves both JSON endpoints and the web dashboard.

| Endpoint | Description |
|---|---|
| `GET /api/v1/health` | Health check and version |
| `GET /api/v1/version` | Application version |
| `GET /api/v1/config` | Current configuration |
| `GET /api/v1/metrics?namespace=&last=24h` | Per-pod metrics |
| `GET /api/v1/metrics/summary?namespace=&last=24h` | Aggregated summary |
| `GET /api/v1/metrics/timeseries?granularity=day&last=7d` | Time-series data |
| `GET /api/v1/namespaces` | List of active namespaces |
| `GET /api/v1/nodes` | Cluster node inventory |
| `GET /api/v1/recommendations?namespace=` | Optimization recommendations |

Interactive API docs are available at `/api/v1/docs` (Swagger UI).

## 📈 Running Reports & Getting Recommendations

The primary way to interact with GreenKube is by using `kubectl exec` to run commands inside the running pod.

### 1. Find your GreenKube pod:

```bash
kubectl get pods -n greenkube
```

(Look for a pod named something like greenkube-7b5...)

### 2. Run an on-demand report:

```bash
# Replace <pod-name> with the name from the previous step
kubectl exec -it <pod-name> -n greenkube -- bash
```

### 3. Run a report:

```bash
greenkube report --daily
```
See the doc or `greenkube report --help` to see more options.

### 4. Get optimization recommendations:

```bash
greenkube recommend
```

## 🏗️ Architecture Summary

GreenKube is composed of:
- **Collectors:** PrometheusCollector, NodeCollector, PodCollector, and OpenCostCollector gather metrics from various cluster services.
- **Estimator:** Converts Prometheus CPU metrics into EnergyMetric objects (Joules) using instance power profiles.
- **Processor:** Orchestrates the pipeline. It groups metrics by Electricity Maps zone, prefetches grid intensity data once per zone per run, and combines all data sources.
- **Calculator:** Converts Joules → kWh → CO2e and uses a per-run cache to avoid redundant intensity lookups.
- **Recommender:** Analyzes the final CombinedMetric data to find "zombie" and "oversized" pods.
- **Repositories:** SQLiteRepository and ElasticsearchRepository provide a stable interface for storing and retrieving carbon intensity data.

Key design goals:
- Be resilient to diverse Prometheus and OpenCost deployments via auto-discovery.
- Use conservative defaults (e.g., default instance power profile) when cluster information is missing, allowing the pipeline to continue.
- Reduce external API calls by caching and prefetching grid intensity data.


## 🤝 Contributing
GreenKube is a community-driven project, and we welcome all contributions! Check out our upcoming `CONTRIBUTING.md` file to learn how to get involved.

* **Report Bugs**: Open an "Issue" with a detailed description.

* **Suggest Features**: Let's discuss them in the GitHub "Discussions".

* **Submit Code**: Make a "Pull Request"!


## 📄 Licence

This project is licensed under the `Apache 2.0 License`. See the `LICENSE` file for more details.
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

## ✨ Features (Version 0.2.0)

### 📊 Dashboard & Visualization
* **Modern Web Dashboard:** Built-in SvelteKit SPA with real-time charts (ECharts), interactive per-pod metrics table, node inventory, and optimization recommendations — all served from the same container as the API.
* **REST API:** Full-featured FastAPI backend with comprehensive endpoints for metrics, nodes, namespaces, recommendations, timeseries, and configuration. OpenAPI docs included at `/api/v1/docs`.

### 📈 Comprehensive Resource Monitoring
* **Multi-Resource Metrics Collection:** Beyond CPU, GreenKube now tracks:
  - **CPU usage** (actual utilization in millicores)
  - **Memory usage** (bytes consumed)
  - **Network I/O** (bytes received/transmitted)
  - **Disk I/O** (bytes read/written)
  - **Storage** (ephemeral storage requests and usage)
  - **Pod restarts** (restart count per container)
  - **GPU usage** (millicores, when available)
* **Energy Estimation:** Calculates pod-level energy consumption (Joules) using Prometheus metrics and a built-in library of cloud instance power profiles.
* **Carbon Footprint Tracking:** Converts energy to CO₂e emissions using real-time or default grid carbon intensity data.

### 🎯 Optimization & Reporting
* **Smart Recommendations:** Identifies optimization opportunities:
  - **Zombie pods** (idle but costly workloads)
  - **Oversized pods** (underutilized CPU/memory)
  - **Rightsizing suggestions** with potential cost and emission savings
* **Pod & Namespace Reporting:** Detailed reports of CO₂e emissions, energy usage, and costs per pod and namespace.
* **Historical Analysis:** Report on any time period (`--last 7d`, `--last 3m`) with flexible grouping (`--daily`, `--monthly`, `--yearly`).
* **Data Export:** Export reports to CSV or JSON for integration with other tools and BI systems.

### 🔧 Infrastructure & Deployment
* **Flexible Data Backends:** Supports PostgreSQL (default/recommended), SQLite (local/dev), and Elasticsearch (production scale) for storing metrics and carbon intensity data.
* **Service Auto-Discovery:** Automatically discovers in-cluster Prometheus and OpenCost services to simplify setup (manually configurable via Helm values).
* **Helm Chart Deployment:** Production-ready Helm chart with PostgreSQL StatefulSet, configurable persistence, RBAC, and health probes.
* **Cloud Provider Support:** Built-in profiles for AWS, GCP, Azure, OVH, and Scaleway with automatic region-to-carbon-zone mapping.


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
- **Dashboard** — KPI cards (CO₂, cost, energy, pods), time-series charts (ECharts), namespace breakdown pie chart, and top pods by emissions/cost
- **Metrics** — Interactive table with sortable and searchable per-pod metrics including energy, cost, and all resource consumption data (CPU, memory, network, disk, storage)
- **Nodes** — Cluster node inventory with CPU/memory capacity bars, hardware profiles, cloud provider info, and carbon zones
- **Recommendations** — Actionable optimization suggestions (zombie pods, rightsizing opportunities) with estimated savings in cost and CO₂e
- **Settings** — Current configuration, API health status, version info, and database connection details

### 🎨 Dashboard Features
- **Real-time updates** with WebSocket support (when available)
- **Responsive design** works on desktop and mobile
- **Dark/light theme** support
- **Export capabilities** for charts and data tables
- **Advanced filtering** by namespace, time range, and resource type

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

GreenKube follows a clean, hexagonal architecture with strict separation between core business logic and infrastructure adapters.

### Core Components

**Collectors** (Input Adapters):
- **PrometheusCollector:** Fetches CPU, memory, network I/O, disk I/O, and restart count metrics via PromQL queries
- **NodeCollector:** Gathers node metadata (zones, instance types, capacity) from Kubernetes API
- **PodCollector:** Collects resource requests (CPU, memory, ephemeral storage) from pod specs
- **OpenCostCollector:** Retrieves cost allocation data for financial reporting
- **ElectricityMapsCollector:** Fetches real-time carbon intensity data by geographic zone

**Processing Pipeline:**
- **Estimator:** Converts Prometheus CPU metrics into EnergyMetric objects (Joules) using cloud instance power profiles
- **Processor:** Orchestrates the entire data collection and processing pipeline:
  - Runs all collectors concurrently via `asyncio.gather`
  - Reconstructs historical node states from database snapshots
  - Groups metrics by carbon zone for efficient intensity lookups
  - Aggregates estimation flags and reasons for transparency
  - Manages per-pod resource maps (CPU, memory, network, disk, storage, restarts)
- **Calculator:** Converts energy (Joules → kWh) to carbon emissions (CO₂e) using grid intensity and PUE
  - Maintains per-run cache of (zone, timestamp) → intensity mappings
  - Supports normalization (hourly/daily/none) for efficient lookups

**Business Logic:**
- **Recommender:** Analyzes CombinedMetric data to identify optimization opportunities:
  - Zombie detection (idle pods consuming resources)
  - Rightsizing analysis (over-provisioned CPU/memory)
  - Autoscaling recommendations based on variability
  - Carbon-aware scheduling opportunities

**Storage** (Output Adapters):
- **Repositories:** Abstract interfaces implemented for multiple backends:
  - **PostgresRepository:** Production-grade persistent storage (asyncpg driver)
  - **SQLiteRepository:** Local development and testing (aiosqlite driver)
  - **ElasticsearchRepository:** High-scale time-series storage and analytics
- **NodeRepository:** Historical node state snapshots for accurate time-range reporting
- **EmbodiedRepository:** Boavizta API integration for hardware embodied emissions

**API & Presentation:**
- **FastAPI Server:** REST API with OpenAPI documentation, CORS support, health checks
- **SvelteKit Dashboard:** Modern SPA with:
  - Server-side rendering (SSR) for fast initial load
  - Client-side navigation for smooth UX
  - Chart.js/ECharts for interactive visualizations
  - Tailwind CSS for responsive design

### Data Flow

1. **Collection Phase** (async/concurrent):
   ```
   Prometheus → CPU, memory, network, disk metrics
   Kubernetes → Node metadata, pod resource requests
   OpenCost → Cost allocation data
   ```

2. **Processing Phase**:
   ```
   Raw metrics → Energy estimation (Joules per pod)
   Node metadata → Cloud zone mapping
   Historical data → Node state reconstruction
   ```

3. **Calculation Phase**:
   ```
   Energy + Grid intensity + PUE → CO₂e emissions
   Metrics + Cost data → Combined metrics
   ```

4. **Analysis Phase**:
   ```
   Combined metrics → Recommendations engine
   Time-series data → Trend analysis
   ```

5. **Storage & Presentation**:
   ```
   Combined metrics → Database (Postgres/SQLite/ES)
   Database → API → Web Dashboard
   API → CLI reports/exports
   ```

### Key Design Principles

- **Async-First:** Fully leverages Python `asyncio` for non-blocking I/O operations
- **Database Agnostic:** Repository pattern abstracts storage implementation
- **Cloud Agnostic:** Supports AWS, GCP, Azure, OVH, Scaleway with extensible mapping
- **Resilient:** Graceful degradation when data sources are unavailable
- **Transparent:** Clear flagging of estimated vs. measured values with reasoning
- **Modular:** Each component is independently testable and replaceable
- **Observable:** Comprehensive logging at all pipeline stages


## 🤝 Contributing
GreenKube is a community-driven project, and we welcome all contributions! Check out our upcoming `CONTRIBUTING.md` file to learn how to get involved.

* **Report Bugs**: Open an "Issue" with a detailed description.

* **Suggest Features**: Let's discuss them in the GitHub "Discussions".

* **Submit Code**: Make a "Pull Request"!


## 📄 Licence

This project is licensed under the `Apache 2.0 License`. See the `LICENSE` file for more details.
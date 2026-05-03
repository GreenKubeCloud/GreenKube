<img src="https://raw.githubusercontent.com/GreenKubeCloud/GreenKube/refs/heads/gh-pages/assets/greenkube-logo.png" alt="GreenKube Logo" height="80">

# GreenKube

**Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.**

GreenKube is an open-source FinGreenOps platform for Kubernetes. It gives DevOps, SRE, and FinOps teams real-time carbon visibility and cost control — without complex setup or expensive SaaS tooling.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Docker Pulls](https://img.shields.io/docker/pulls/greenkube/greenkube)](https://hub.docker.com/r/greenkube/greenkube)
[![Python Coverage](https://img.shields.io/badge/coverage%20python-90%25-brightgreen)](tests/)
[![Frontend Coverage](https://img.shields.io/badge/coverage%20frontend-93%25-brightgreen)](frontend/tests/)
[![Tests](https://img.shields.io/badge/tests-1076%20passed-brightgreen)](tests/)
[![Build in Public](https://img.shields.io/badge/Build%20in-Public-blueviolet)](CHANGELOG.md)

>**Live demo:** [demo.greenkube.cloud](https://demo.greenkube.cloud) — explore the full dashboard with realistic sample data, no install required.

---

## What it does

- **Estimates** the energy consumption and CO₂e emissions of each Kubernetes workload, using CPU metrics from Prometheus and cloud instance power profiles.
- **Visualises** those metrics in a real-time web dashboard with per-pod data, node inventory, and namespace breakdowns.
- **Recommends** concrete optimizations to simultaneously reduce cloud spend and carbon footprint — rightsizing, zombie pod cleanup, autoscaling candidates, and more.
- **Reports** historical emissions and cost data, exportable as CSV or JSON for CSRD/ESRS E1 compliance.
- **Integrates** with Prometheus and Grafana to expose GreenKube metrics alongside the rest of your cluster observability stack.

---

## Screenshots

| Grafana dashboard |
|------------------|
| <img src="assets/grafana-dashboard-top-panel.png" width="460"> |

### Frontend screenshots 

| Dashboard | Metrics |
|----------|----------|
| <img src="assets/demo-dashboard.png" width="460"> | <img src="assets/demo-metrics.png" width="460"> |

| Nodes | Recommendations |
|----------|----------|
| <img src="assets/demo-nodes.png" width="460"> | <img src="assets/demo-recommendations.png" width="460"> |

| Report | Settings |
|----------|----------|
| <img src="assets/demo-report.png" width="460"> | <img src="assets/demo-settings.png" width="460"> |

---

## Documentation

| Document | Description |
|----------|-------------|
| [Architecture](docs/architecture.md) | Technical architecture, data flow, and component breakdown |
| [Power estimation methodology](docs/power_estimation_methodology.md) | How energy and CO₂e are calculated |
| [Configuration](docs/configuration.md) | All Helm values and environment variables |
| [API Reference](docs/api.md) | REST API endpoints, parameters, and examples |
| [CLI Reference](docs/cli.md) | `greenkube report`, `recommend`, and other CLI commands |
| [Prometheus & Grafana](docs/prometheus-grafana.md) | ServiceMonitor setup and Grafana dashboard import |
| [Sustainability score](docs/sustainability-score.md) | How the 0–100 composite score is computed |
| [Changelog](CHANGELOG.md) | Version history |

---

## Installation

The recommended deployment method is the official Helm chart.

```bash
helm repo add greenkube https://GreenKubeCloud.github.io/GreenKube
helm repo update
helm install greenkube greenkube/greenkube \
  -n greenkube \
  --create-namespace
```

Once deployed, access the dashboard:

```bash
kubectl port-forward svc/greenkube-api 8000:8000 -n greenkube
# Open http://localhost:8000
```

### Key configuration variables

Create a `my-values.yaml` to customise your deployment:

```yaml
secrets:
  # Electricity Maps API token for real-time grid carbon intensity.
  # Free token: https://www.electricitymaps.com/
  electricityMapsToken: ""

config:
  prometheus:
    url: ""              # Leave empty for automatic in-cluster discovery
    queryRangeStep: 5m   # Metric collection window
```

Apply it:

```bash
helm upgrade greenkube greenkube/greenkube \
  -n greenkube \
  -f my-values.yaml
```

For the full list of available variables, see the [Configuration reference](docs/configuration.md).

### Dependencies

GreenKube auto-discovers the following services. No manual configuration is required in most cases.

| Service | Purpose | Required? |
|---------|---------|-----------|
| Prometheus | CPU, memory, network, disk metrics | Strongly recommended |
| OpenCost | Cost allocation data | Optional |

### On-premises clusters

Cloud providers expose zone labels on nodes automatically. On bare-metal clusters, label your nodes manually:

```bash
kubectl label nodes --all topology.kubernetes.io/zone=FR
```

Then set `config.cloudProvider: on-prem` and `config.defaultZone: FR` in your values. See the [Configuration reference](docs/configuration.md#on-premises-and-bare-metal-clusters) for details.

---

## Try the demo locally

```bash
docker run --rm -p 9000:9000 greenkube/greenkube demo --no-browser --port 9000
# Open http://localhost:9000
```

This generates 30 days of sample data for 22 pods across 5 namespaces and 3 nodes, including carbon emissions, costs, and optimization recommendations.

---

## Contributing

Contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md) to get started.

```bash
git clone https://github.com/GreenKubeCloud/GreenKube.git
cd GreenKube
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,test]"
pytest
```

---

## Licence

Licensed under the [Apache 2.0 License](LICENSE).

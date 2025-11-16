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

## ✨ Features (Community Edition v0.1.3)

* **Prometheus-Based Energy Estimation:** Calculates pod-level energy consumption (Joules) using CPU usage data from Prometheus and a built-in library of instance power profiles.
* **Optimization Recommendations:** Identifies "zombie" pods (idle but costly) and "oversized" pods (underutilized CPU) to help you rightsize and reduce waste.
* **Pod & Namespace Reporting:** Generates detailed reports of CO2e emissions, energy usage, and (optional) costs per pod and namespace.
* **Flexible Data Backends:** Supports SQLite (default) and Elasticsearch for storing and querying historical carbon intensity data.
* **Historical Analysis:** Report on energy and carbon usage over any time period (`--last 7d`, `--last 3m`) with flexible grouping (`--daily`, `--monthly`, etc.).
* **Service Auto-Discovery:** Automatically discovers in-cluster Prometheus and OpenCost services to simplify setup (can be manually overridden).
* **Helm Chart Deployment:** Easily deploy and configure GreenKube in any Kubernetes cluster via a public Helm repository.
* **Data Export:** Export reports to CSV or JSON for integration with other tools.

## 🚀 Installation & Usage

The recommended way to install GreenKube is via the official Helm chart.

### 1. Add the GreenKube Helm Repository

First, add the GreenKube chart repository to your local Helm setup:

```bash
helm repo add greenkube https://GreenKubeCloud.github.io/GreenKube
helm repo update
```

### 2. Configure Your Installation

GreenKube requires an API token from Electricity Maps to fetch grid carbon intensity data. If not provided, a default value will be used for every zone.

#### i. Create a file named `my-values.yaml`.

Add your API token to the file. You can also configure your database type (e.g., elasticsearch) and other settings here.

`my-values.yaml`:

```yaml
# Uncomment to use Elasticsearch instead of the default SQLite (recommended)
# config:
#   db:
#     type: "elasticsearch"

# Configure the Elasticsearch hosts
# elasticsearch:
#   hosts: "http://your-elasticsearch-host:9200"

secrets:
  # Get your API token from [https://www.electricitymaps.com/](https://www.electricitymaps.com/)
  # If not provided, a default value will be used for every zone
  electricityMapsToken: "YOUR_API_TOKEN_HERE"
  # Provide credentials in the secrets section
  # elasticsearch:
  #   user: "elastic"
  #   password: "your-password"

# Uncomment to manually set your Prometheus URL
# (If left empty, GreenKube will try to auto-discover it)
# config:
#   prometheus:
#     url: "[http://prometheus-k8s.monitoring.svc.cluster.local:9090](http://prometheus-k8s.monitoring.svc.cluster.local:9090)"
```

#### ii. Install the Chart

Install the Helm chart into a dedicated namespace (e.g., `greenkube`):

```bash
helm install greenkube greenkube/greenkube \
  -f my-values.yaml \
  -n greenkube \
  --create-namespace
```

This will deploy GreenKube, which runs as a service (greenkube start) to continuously collect carbon intensity data.

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
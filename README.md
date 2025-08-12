# GreenKube 🌍♻️

![GreenKube Banner](https://placehold.co/1200x400/2D8A5F/FFFFFF?text=GreenKube&font=raleway)

**Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure. Make your cloud operations both cost-effective and environmentally responsible.**

GreenKube is an open-source tool designed to help DevOps, SRE, and FinOps teams navigate the complexity of sustainability reporting (CSRD) and optimize their cloud costs (FinOps) through better energy efficiency (GreenOps).

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![GitHub Stars](https://img.shields.io/github/stars/GreenKubeCloud/greenkube?style=social)](https://github.com/GreenKubeCloud/greenkube/stargazers)
[![Build in Public](https://img.shields.io/badge/Build%20in-Public-blueviolet)](https://github.com/GreenKubeCloud/greenkube)

---

## 🎯 Mission

The EU's Corporate Sustainability Reporting Directive (CSRD) requires companies to report the carbon footprint of their value chain—including cloud services (Scope 3). GreenKube addresses this urgent need by providing tools to:

1.  **Measure** the energy consumption and CO₂e emissions of each Kubernetes workload.
2.  **Report** these metrics in a format aligned with regulatory requirements (ESRS E1).
3.  **Optimize** infrastructure to simultaneously reduce cloud bills and environmental impact.

## ✨ Features (Community Edition v0.1)

* **Carbon Footprint Reporting**: Calculates CO2e emissions per pod, namespace, and for the entire cluster.
* **Kepler Integration**: Uses granular energy consumption metrics from the CNCF project [Kepler](https://github.com/sustainable-computing-io/kepler).
* **OpenCost Alignment**: Aligns with industry standards for cost visibility.
* **Carbon Intensity Data**: Integrates with public APIs to fetch real-time carbon intensity from the local power grid.
* **Command-Line Export**: Generates simple and clear reports directly from your terminal.

## 🚀 Quick Start

### Prerequisites

1.  Python 3.9+
2.  A Kubernetes cluster with [Kepler](https://github.com/sustainable-computing-io/kepler) installed and exporting its metrics via a Prometheus service.

### Installation

*Coming soon once the first package is published.* For now, install from the source:

```bash
git clone [https://github.com/GreenKubeCloud/greenkube.git](https://github.com/GreenKubeCloud/greenkube.git)
cd greenkube
pip install -e .
```

### Utilisation de base

```bash
# Display a summary carbon footprint report for all namespaces
greenkube report

# Display a detailed report for a specific namespace
greenkube report --namespace my-production-app

# Export raw data in CSV format
greenkube export --format csv --output report.csv
```

## 🤝 Contribution
GreenKube is a community-driven project, and we welcome all contributions! Check out our upcoming **CONTRIBUTING.md** file to learn how to get involved.

* **Report Bugs**: Open an "Issue" with a detailed description.

* **Suggest Features**: Let's discuss them in the GitHub "Discussions".

* **Submit Code**: Make a "Pull Request"!


## 📄 Licence

This project is licensed under the **Apache 2.0 License**. See the LICENSE file for more details.
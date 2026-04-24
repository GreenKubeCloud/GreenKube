# Configuration Reference

GreenKube is configured exclusively through environment variables, which are managed via the Helm chart's `values.yaml`. All available options are listed below.

## Helm values

The full `values.yaml` is self-documented. The most important parameters are grouped below.

### Image

```yaml
image:
  repository: greenkube/greenkube
  tag: 0.2.9
  pullPolicy: IfNotPresent
```

### General configuration (`config`)

| Key | Default | Description |
|-----|---------|-------------|
| `config.logLevel` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `config.clusterName` | `""` | Cluster name used as a label in Prometheus metrics |
| `config.cloudProvider` | `unknown` | Cloud provider (`aws`, `gcp`, `azure`, `ovh`, `scaleway`, `on-prem`, `unknown`) |
| `config.defaultZone` | `""` | Electricity Maps zone code (e.g. `FR`, `DE`, `US-CAL-CISO`). Auto-discovered from node labels if empty. |
| `config.defaultIntensity` | `500.0` | Fallback grid carbon intensity in gCO₂e/kWh when zone cannot be determined |
| `config.normalizationGranularity` | `hour` | Carbon intensity lookup granularity (`hour`, `day`, `none`) |
| `config.nodeAnalysisInterval` | `5m` | Interval for analysing node state |
| `config.nodeDataMaxAgeDays` | `30` | Maximum age for historical node snapshots |
| `config.k8sRequestTimeout` | `30` | Timeout in seconds for Kubernetes API calls |

### Data retention

| Key | Default | Description |
|-----|---------|-------------|
| `config.metricsCompressionAgeHours` | `24` | Age in hours after which 5-min raw metrics are compressed into hourly aggregates |
| `config.metricsRawRetentionDays` | `7` | Days to retain raw metrics before deletion after compression |
| `config.metricsAggregatedRetentionDays` | `-1` | Days to retain hourly aggregates. `-1` means indefinite (recommended for CSRD/ESRS E1 yearly reporting) |

### Database (`config.db`)

| Key | Default | Description |
|-----|---------|-------------|
| `config.db.type` | `postgres` | Backend: `postgres` (recommended) or `sqlite` (dev/standalone) |
| `config.db.path` | `/data/greenkube_data.db` | SQLite file path (only used when `db.type` is `sqlite`) |

### Boavizta API (`config.boavizta`)

| Key | Default | Description |
|-----|---------|-------------|
| `config.boavizta.url` | `https://api.boavizta.org` | Boavizta API endpoint for embodied emissions |
| `config.boavizta.defaultEmbodiedEmissionsKg` | `350` | Fallback embodied emissions in kg CO₂e when the instance type is not recognised |

### Prometheus & OpenCost integration (`config.prometheus`, `config.opencost`)

By default, GreenKube auto-discovers Prometheus and OpenCost in the cluster. Manual override:

```yaml
config:
  prometheus:
    url: "http://prometheus-k8s.monitoring.svc.cluster.local:9090"
  opencost:
    url: "http://opencost.opencost.svc.cluster.local:9003"
```

### Secrets (`secrets`)

| Key | Description |
|-----|-------------|
| `secrets.electricityMapsToken` | Electricity Maps API token for real-time grid intensity. Without it, the default intensity is used. Get a free token at [electricitymaps.com](https://www.electricitymaps.com/) |
| `secrets.existingSecret` | Name of an existing Kubernetes Secret to use instead of creating one from `values.yaml` |

### Monitoring (`monitoring`)

```yaml
monitoring:
  serviceMonitor:
    enabled: false       # Set to true if using the Prometheus Operator (kube-prometheus-stack)
    namespace: monitoring
    interval: 30s
  networkPolicy:
    enabled: false       # Allow Prometheus to scrape the GreenKube API port
    prometheusNamespace: monitoring
```

### PostgreSQL StatefulSet (`postgresql`)

GreenKube ships with an optional bundled PostgreSQL StatefulSet. Adjust storage and credentials as needed:

```yaml
postgresql:
  enabled: true
  storage:
    size: 5Gi
  auth:
    database: greenkube
    username: greenkube
    # password is managed via secrets
```

## On-premises and bare-metal clusters

Cloud providers automatically expose zone labels on nodes (`topology.kubernetes.io/zone`). On-premises clusters require manual configuration:

```bash
# Label nodes with their Electricity Maps zone code
kubectl label nodes --all topology.kubernetes.io/zone=FR
```

Then set the following in your `values.yaml`:

```yaml
config:
  cloudProvider: on-prem
  defaultZone: FR
```

If the cluster spans multiple geographic locations, label each node individually.

## Applying a custom configuration

```bash
helm upgrade greenkube greenkube/greenkube \
  -n greenkube \
  -f my-values.yaml
```

Minimal `my-values.yaml` example:

```yaml
secrets:
  electricityMapsToken: "YOUR_TOKEN_HERE"

config:
  clusterName: "prod-eu-west"
  cloudProvider: aws
```

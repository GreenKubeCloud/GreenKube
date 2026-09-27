# Configuration Reference

GreenKube is configured exclusively through environment variables, which are managed via the Helm chart's `values.yaml`. All available options are listed below.

## Helm values

The full `values.yaml` is self-documented. The most important parameters are grouped below.

### Image

```yaml
image:
  repository: greenkube/greenkube
  tag: 0.3.0
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
| `config.electricityProvider` | `electricity_maps` | Grid intensity data provider: `electricity_maps` (default) or `wattnet` (EU-only, free — see [docs/wattnet.md](wattnet.md)) |
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
| `config.boavizta.defaultEmbodiedEmissionsKg` | `100` | Fallback embodied emissions in kg CO₂e when the instance type is not recognised |

### Prometheus & OpenCost integration (`config.prometheus`, `config.opencost`)

By default, GreenKube auto-discovers Prometheus and OpenCost in the cluster. Manual override:

```yaml
config:
  prometheus:
    url: "http://prometheus-k8s.monitoring.svc.cluster.local:9090"
  opencost:
    url: "http://opencost.opencost.svc.cluster.local:9003"
```

### Recommendation engine (`config.recommendations`)

Analyzer thresholds and source selection live under `config.recommendations`.
The values are wired to `RECOMMENDATION_*` environment variables. Key entries:

| Helm value | Environment variable | Default | Description |
|---|---|---|---|
| `recommendations.vpaEnabled` | `RECOMMENDATION_VPA_ENABLED` | `true` | Read recommendation-mode VPAs (`updateMode: Off`). The source detects the CRD and skips cleanly when absent; native rightsizing for the same workload is then suppressed. |
| `recommendations.karpenterEnabled` | `RECOMMENDATION_KARPENTER_ENABLED` | `true` | Karpenter NodePool consolidation recommendations. Auto-detects the CRDs. |
| `recommendations.sourcePriority` | `RECOMMENDATION_SOURCE_PRIORITY` | `vpa,karpenter,greenkube` | Arbitration precedence (highest first). |
| `recommendations.ttlDays` | `RECOMMENDATION_TTL_DAYS` | `14` | Recommendation expiry. |
| `recommendations.verification.windowHours` | `VERIFICATION_WINDOW_HOURS` | `72` | Post-apply observation window before the outcome is verified. |
| `recommendations.verification.minSavingsRatio` | `VERIFICATION_MIN_SAVINGS_RATIO` | `0.5` | Measured savings required to pass the cost gate. |
| `recommendations.verification.usageHeadroom` | `VERIFICATION_USAGE_HEADROOM` | `1.1` | p95 usage ceiling vs the proposed request. |
| `recommendations.verification.lifecycleInterval` | `RECOMMENDATION_LIFECYCLE_INTERVAL` | `5m` | Apply detection / verification / expiry job interval. |
| `recommendations.git.provider` | `GIT_PROVIDER` | `github` | PR bot provider: `github`, `gitlab` or `gitea`. |
| `recommendations.git.apiBaseUrl` | `GIT_API_BASE_URL` | `""` | API override for self-hosted instances. |
| `recommendations.git.defaultBranch` | `GIT_DEFAULT_BRANCH` | `main` | Fallback base branch for pull requests. |

### Secrets (`secrets`)

| Key | Description |
|-----|-------------|
| `secrets.electricityMapsToken` | Electricity Maps API token for real-time grid intensity. Without it, the default intensity is used. Get a free token at [electricitymaps.com](https://www.electricitymaps.com/) |
| `secrets.wattnetEmail` | Wattnet account email (only when `config.electricityProvider` is `wattnet`). Register for free at [api.wattnet.eu/token-request/register](https://api.wattnet.eu/token-request/register) |
| `secrets.wattnetPassword` | Wattnet account password (only when `config.electricityProvider` is `wattnet`) |
| `secrets.gitToken` | Git personal access token used by the recommendation PR bot. Leave empty to disable PR automation. |
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

### PostgreSQL StatefulSet (`postgres`)

GreenKube ships with an optional bundled PostgreSQL StatefulSet. Adjust storage and credentials as needed:

```yaml
postgres:
  enabled: true
  image:
    repository: postgres
    tag: 18-alpine
    pullPolicy: IfNotPresent
  auth:
    username: greenkube
    # Leave empty to generate a random password on first install (preserved
    # across upgrades), or set it explicitly.
    password: ''
    database: greenkube
  persistence:
    enabled: true
    size: 10Gi
    storageClassName: ''
  resources:
    limits:
      cpu: 500m
      memory: 512Mi
    requests:
      cpu: 100m
      memory: 128Mi
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

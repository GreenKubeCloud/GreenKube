# Configuration Reference

The Helm chart maps `values.yaml` settings to GreenKube's environment-based configuration. The chart's [`values.yaml`](../helm-chart/values.yaml) is the authoritative list of deployment options; this page summarizes the commonly used settings. Selected integration URLs and tokens can also be changed at runtime from the Settings page. Runtime updates affect the running process and are persisted to the Kubernetes Secret on a best-effort basis; use Helm values for durable configuration.

## Helm values

The chart supports two deployment profiles:

- `profile: standalone` (default) deploys the collector and API together.
- `profile: production` renders separate API, controller, and automation deployments, including the worker that executes queued pull-request operations. The standalone profile does not run that worker. The default profile is not a substitute for production authentication or network controls.

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
| `config.environment` | `development` | Runtime environment. Production configuration requires an API key when `config.apiAuthMode` is `api_key`. |
| `config.apiAuthMode` | `api_key` | API authentication contract. API-key authentication is the currently supported request middleware mode. |
| `config.clusterName` | `""` | Cluster name used as a label in Prometheus metrics |
| `config.cloudProvider` | `unknown` | Cloud provider (`aws`, `gcp`, `azure`, `ovh`, `scaleway`, `on-prem`, `unknown`) |
| `config.defaultZone` | `""` | Fallback Electricity Maps zone code for nodes whose zone or region cannot be mapped (e.g. `FR`, `DE`, `US-CAL-CISO`) |
| `config.defaultIntensity` | `500.0` | Global fallback grid intensity in gCO₂e/kWh when no zone-specific intensity is available |
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
| `config.metricsAggregatedRetentionDays` | `-1` | Days to retain hourly aggregates. `-1` means indefinite (useful for multi-year trend analysis) |

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

When the URLs are empty, GreenKube attempts in-cluster discovery for Prometheus and OpenCost. Set explicit URLs when discovery does not match your installation:

```yaml
config:
  prometheus:
    url: "http://prometheus-k8s.monitoring.svc.cluster.local:9090"
  opencost:
    url: "http://opencost.opencost.svc.cluster.local:9003"
```

### Recommendation engine (`config.recommendations`)

Analyzer thresholds and source selection live under `config.recommendations`. The values are wired to `RECOMMENDATION_*` environment variables. Key entries:

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
| `secrets.apiKey` | API bearer token. Required for production when using `config.apiAuthMode: api_key`; with an empty key, development API routes are open. |
| `secrets.electricityMapsToken` | Electricity Maps API token when `config.electricityProvider` is `electricity_maps`. Without it, GreenKube uses bundled zone-specific fallback data when available, then the configured global intensity. Get a token at [electricitymaps.com](https://www.electricitymaps.com/). |
| `secrets.wattnetEmail` | Wattnet account email (only when `config.electricityProvider` is `wattnet`). Register for free at [api.wattnet.eu/token-request/register](https://api.wattnet.eu/token-request/register) |
| `secrets.wattnetPassword` | Wattnet account password (only when `config.electricityProvider` is `wattnet`) |
| `secrets.boaviztaToken` | Optional Boavizta API token. |
| `secrets.gitToken` | Git personal access token used by the recommendation PR bot. Leave empty to disable PR automation; a running automation worker is also required to execute queued operations. |
| `secrets.existingSecret` | Name of an existing Kubernetes Secret to use instead of creating one from `values.yaml` |
| `secrets.dbConnectionString` | PostgreSQL connection string when using an external PostgreSQL instance (`postgres.enabled: false`). |
| `secrets.prometheus.username`, `secrets.prometheus.password`, `secrets.prometheus.bearerToken` | Optional Prometheus authentication credentials. |

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

### API authentication and runtime service settings

The chart defaults to `environment: development`, `apiAuthMode: api_key`, and an empty `secrets.apiKey`, so protected API routes are open in the default configuration. Do not expose that configuration publicly. For a production installation, set a non-empty API key through a Kubernetes Secret and use network controls; see the [API authentication notes](api.md#authentication).

The Settings page sends integration changes to `POST /api/v1/config/services`. The API updates the running process and attempts to patch the mounted Secret. Check the response's `X-Configuration-Persisted` header; if persistence fails, the change is in memory only and will not survive a restart. Helm remains the recommended source of truth for durable values.

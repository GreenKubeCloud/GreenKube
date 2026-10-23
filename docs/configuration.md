# Configuration Reference

The Helm chart maps `values.yaml` settings to GreenKube's environment-based configuration. The chart's [`values.yaml`](../helm-chart/values.yaml) is the authoritative list of deployment options; this page summarizes the commonly used settings. For a step-by-step installation, see the [Installation guide](installation.md). Selected integration URLs and tokens can also be changed at runtime from the Settings page. Runtime updates affect the running process and are persisted to the Kubernetes Secret on a best-effort basis; use Helm values for durable configuration.

## Helm values

The chart supports two deployment profiles:

- `profile: standalone` (default) deploys the collector and API together. When automation is enabled, the worker runs inline in the API process.
- `profile: production` renders separate API, controller, and (when automation is enabled) automation worker deployments. The controller uses a Kubernetes Lease for leader election and the migration Job runs before the API is considered ready. The standalone profile does not run a separate worker.

The default values use `profile: standalone`, `config.environment: production`, `config.apiAuthMode: api_key`, and a generated API key. The default profile is not a substitute for production authentication or network controls.

### Image

```yaml
image:
  repository: greenkube/greenkube
  tag: 0.3.0
  # Optional: pin the exact image instead of using a tag.
  # digest: sha256:<64 hexadecimal characters>
  pullPolicy: IfNotPresent
```

`image.digest` renders `repository@digest` and takes precedence over `image.tag`. Do not put a `sha256:` digest in `image.tag`; the chart rejects that with an actionable error because Kubernetes would otherwise report `InvalidImageName`.

### General configuration (`config`)

| Key | Default | Description |
|-----|---------|-------------|
| `config.logLevel` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `config.environment` | `production` | Runtime environment. Production configuration fails closed when API-key authentication is not configured. |
| `config.apiAuthMode` | `api_key` | API authentication contract. API-key authentication is the currently supported request middleware mode. |
| `config.serviceUrlAllowlist` | `[]` | Exact hostnames permitted for runtime service URL overrides. Configured Prometheus/OpenCost/Boavizta hosts are also allowed. |
| `config.clusterName` | `""` | Cluster name used as a label in Prometheus metrics |
| `config.cloudProvider` | `unknown` | Cloud provider (`aws`, `gcp`, `azure`, `ovh`, `scaleway`, `on-prem`, `unknown`) |
| `config.defaultZone` | `""` | Fallback Electricity Maps zone code for nodes whose zone or region cannot be mapped (e.g. `FR`, `DE`, `US-CAL-CISO`) |
| `config.defaultIntensity` | `500.0` | Global fallback grid intensity in gCO₂e/kWh when no zone-specific intensity is available |
| `config.electricityProvider` | `electricity_maps` | Grid intensity data provider: `electricity_maps` (default) or `wattnet` (EU-only, free — see [docs/wattnet.md](wattnet.md)) |
| `config.normalizationGranularity` | `hour` | Carbon intensity lookup granularity (`hour`, `day`, `none`) |
| `config.nodeAnalysisInterval` | `5m` | Interval for analysing node state |
| `config.nodeDataMaxAgeDays` | `30` | Maximum age for historical node snapshots |
| `config.k8sRequestTimeout` | `30` | Timeout in seconds for Kubernetes API calls |
| `config.kubernetesMetricsApiEnabled` | `true` (Helm) | Read per-container CPU and memory from the read-only `metrics.k8s.io` PodMetrics API. Requires an available PodMetrics APIService; if unavailable, GreenKube logs a warning and continues with other metric sources. The chart grants only `get/list` on `pods.metrics.k8s.io`; it does not grant `nodes/proxy`. |

### API server (`config.api`)

| Key | Default | Description |
|-----|---------|-------------|
| `config.api.enabled` | `true` | Serve the REST API and the SPA frontend. |
| `config.api.host` | `0.0.0.0` | API bind address inside the container. |
| `config.api.port` | `8000` | API/UI container and Service port. |
| `config.api.livenessProbe` | enabled | HTTP liveness probe settings (`path: /api/v1/health`). |
| `config.api.readinessProbe` | enabled | HTTP readiness probe settings (`path: /api/v1/health`). |
| `config.corsOrigins` | `*` | Allowed CORS origins. Prefer same-origin UI/API deployments. |
| `config.apiRateLimit` | `60/minute` | SlowAPI rate limit for API endpoints. |
| `config.rootPath` | `""` | Sub-path prefix when served behind a reverse proxy. |
| `config.metricsListMaxRangeDays` | `30` | Maximum raw `/metrics` listing window; wider ranges use report endpoints. |

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
| `config.db.schema` | `public` | PostgreSQL schema. |
| `config.db.sslMode` | `disable` | PostgreSQL SSL mode (`disable`, `require`, `verify-full`, ...). |
| `config.db.poolMinSize` | `1` | PostgreSQL connection-pool minimum. |
| `config.db.poolMaxSize` | `10` | PostgreSQL connection-pool maximum. |
| `config.db.statementTimeoutMs` | `30000` | Per-statement PostgreSQL timeout. |
| `config.persistence.enabled` | `false` | Persist the SQLite database on a PVC instead of an `emptyDir`. |
| `config.persistence.size` | `10Gi` | SQLite PVC size. |

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
| `config.recommendations.vpaEnabled` | `RECOMMENDATION_VPA_ENABLED` | `true` | Read recommendation-mode VPAs (`updateMode: Off`). The source detects the CRD and skips cleanly when absent; native rightsizing for the same workload is then suppressed. |
| `config.recommendations.karpenterEnabled` | `RECOMMENDATION_KARPENTER_ENABLED` | `true` | Karpenter NodePool consolidation recommendations. Auto-detects the CRDs. |
| `config.recommendations.maxMetricRows` | `RECOMMENDATION_MAX_METRIC_ROWS` | `750000` | Hard bound on combined and container rows loaded by one recommendation scan. The lookback window is shortened automatically before loading above this bound. |
| `config.recommendations.sourcePriority` | `RECOMMENDATION_SOURCE_PRIORITY` | `vpa,karpenter,greenkube` | Arbitration precedence (highest first). |
| `config.recommendations.ttlDays` | `RECOMMENDATION_TTL_DAYS` | `14` | Recommendation expiry. |
| `config.recommendations.verification.windowHours` | `VERIFICATION_WINDOW_HOURS` | `72` | Post-apply observation window before the outcome is verified. |
| `config.recommendations.verification.minSavingsRatio` | `VERIFICATION_MIN_SAVINGS_RATIO` | `0.5` | Measured savings required to pass the cost gate. |
| `config.recommendations.verification.usageHeadroom` | `VERIFICATION_USAGE_HEADROOM` | `1.1` | p95 usage ceiling vs the proposed request. |
| `config.recommendations.verification.lifecycleInterval` | `RECOMMENDATION_LIFECYCLE_INTERVAL` | `5m` | Apply detection / verification / expiry job interval. |

### GitOps automation (`automation`)

Automation is disabled by default. The full workflow, supported source layouts, and security model are documented in [GitOps automation](automation.md). The most important values are:

| Key | Default | Description |
|-----|---------|-------------|
| `automation.enabled` | `false` | Enable discovery, planning, PR operations, sync observation, and apply detection. |
| `automation.mode` | profile-dependent | `inline` (standalone), `worker` (production), or `disabled`. Empty uses the profile default. |
| `automation.discoveryInterval` | `5m` | Discovery refresh cadence (`1s`–`1h`). |
| `automation.bindingTtl` | `30m` | Binding and manual-resolution freshness (`1s`–`24h`). |
| `automation.prPollIntervalSeconds` | `120` | PR polling fallback interval (10–86400). |
| `automation.syncPollIntervalSeconds` | `30` | Controller sync observation interval (10–86400). |
| `automation.syncTimeoutSeconds` | `1800` | Time before a merged but unsynced plan becomes `sync_failed` (60–86400). |
| `automation.workerMetricsPort` | `8001` | Authenticated metrics listener for the dedicated worker. |
| `automation.providers` | `[]` | Git provider definitions. See below. |
| `automation.webhooks.enabled` | `false` | Enable signed provider webhook ingestion. |
| `automation.policy` | See below | Risk, confidence, sample, coverage, PR-budget, cooldown, and freeze gates. |
| `automation.pullRequests` | See below | Draft, label, reviewer, and team-reviewer options. |

Configure providers through Secret references:

```yaml
automation:
  enabled: true
  mode: "" # Defaults to inline in standalone and worker in production.
  providers:
    - id: github-main
      type: github
      apiBaseUrl: https://api.github.com
      secretName: greenkube-git
      secretKey: github-token
      repositoryAllowlist:
        - "acme/gitops"
```

Each provider needs a non-empty repository allowlist; an empty allowlist denies all repositories. Glob matching is segment-aware: `acme/*` matches `acme/repo`, not `acme/team/repo`. Only the provider ID, API base URL, Secret reference, and allowlist are rendered into the ConfigMap. The token stays in the referenced Kubernetes Secret and is mounted read-only at `/etc/greenkube/git-providers/<secret-name>/<secret-key>`; when `secretNamespace` is set, the mount path includes that namespace before the Secret name. The Secret must be in the Helm release namespace. Argo CD and Flux discovery select a configured provider only when its allowlist matches the source repository. Preview and approval remain unavailable for missing credentials, unsupported renderers, or ambiguous sources.

The nested `tokenSecret: {name, key, namespace}` form is also accepted for compatibility, and `repositories` is accepted as an alias for `repositoryAllowlist`. Do not mix conflicting flat and nested values.

#### Upgrading from the legacy single-provider configuration

`secrets.gitToken` and `config.recommendations.git.*` are retired and are not migrated automatically. Before upgrading, create a namespaced Secret without placing the token in Helm values:

```sh
kubectl create secret generic greenkube-git \
  --namespace greenkube \
  --from-file=github-token=./github-token
```

Then configure the provider registry explicitly:

```yaml
automation:
  providers:
    - id: github-main
      type: github
      apiBaseUrl: https://api.github.com
      secretName: greenkube-git
      secretKey: github-token
      repositoryAllowlist:
        - "acme/gitops"
```

Remove the obsolete values from the release values file. If a token was previously stored in Helm values or an older chart Secret, rotate it and create a new Kubernetes Secret before enabling automation.

Flux HelmRelease discovery also reads live `apps/v1` Deployments, StatefulSets, and DaemonSets to resolve Helm release ownership. These cluster-wide reads use paginated requests (250 objects per page by default, at most 20 pages per kind); if Kubernetes reports more pages than the bound, the refresh fails and retries rather than reconciling a partial workload inventory. The service account needs `list` access to these workload kinds, already granted by the chart's default ClusterRole.

### Signed Git provider webhooks

Webhooks are disabled by default. Enable them with provider-specific read-only Secret projections into API containers only. Set `webhookSecretName` and `webhookSecretKey` on each provider entry in `automation.providers`; each key is projected to a filename matching that provider ID under `/etc/greenkube/webhooks`. A webhook Secret must be in the Helm release namespace, and each provider must use a distinct Secret key.

For a single provider, the legacy top-level Secret reference remains supported when paired with `automation.webhooks.providerId` (or when exactly one provider is configured):

```yaml
automation:
  webhooks:
    enabled: true
    providerId: github-main
    secretName: greenkube-webhooks
    secretKey: hmac
    # secretNamespace is optional and must match the Helm release namespace.
```

`GET /api/v1/automation/status` reports worker heartbeat age, queue depth, plan backlog, discovery freshness, renderer configuration, and persisted provider capability/preflight state. `ready` is true only when automation is enabled and a worker heartbeat is fresh. The renderer section also reports whether the optional egress firewall is enabled; NetworkPolicy enforcement still depends on the cluster CNI.

The mount directory path, not Secret values, is written to the ConfigMap as `GITOPS_WEBHOOK_SECRET_FILE`. The `POST /api/v1/webhooks/{provider_id}` route exempts only that exact route shape from the application bearer API key; it still requires the matching provider's native GitHub/Gitea HMAC-SHA256 signature or GitLab shared-token comparison before persisting a delivery. Use TLS, especially for GitLab, whose token does not authenticate the request body. Provider-native webhook signatures do not carry a signed timestamp, so delivery IDs provide deduplication rather than timestamp freshness protection.

`automation.discoveryInterval` maps to `GITOPS_DISCOVERY_INTERVAL` (default `5m`, accepted range `1s`–`1h`) and controls the periodic GitOps discovery refresh. `automation.bindingTtl` maps to `GITOPS_BINDING_TTL` (default `30m`, accepted range `1s`–`24h`) and bounds binding and manual-resolution freshness. Both accept an integer followed by `ms`, `s`, `m`, or `h`.

`automation.prPollIntervalSeconds` maps to `GITOPS_PR_POLL_INTERVAL` (seconds; default `120`, accepted range `10`–`86400`). The plan worker runs the polling fallback immediately and checks at most 100 linked open PR records per cycle, re-fetching provider state before lifecycle updates. The default Helm deployment remains at one plan-worker replica. Custom multi-replica deployments coordinate through a database-backed singleton lease with a monotonically increasing fencing token: non-owners skip polling, the owner renews its 90-second lease before each provider call, and each provider call is bounded to 30 seconds. A crashed worker's lease expires for takeover; lease persistence errors fail closed and are surfaced in worker logs.

`automation.syncPollIntervalSeconds` maps to `GITOPS_SYNC_POLL_INTERVAL` (default `30` seconds, range `10`–`86400`); `automation.syncTimeoutSeconds` maps to `GITOPS_SYNC_TIMEOUT` (default `1800` seconds, range `60`–`86400`). The single-replica controller observes a merged commit through its exact Argo CD/Flux source and readiness state. Merged actions enter `sync_pending`; a synced plan moves to live per-container verification, while a plan still unsynced after the timeout moves to `sync_failed`, never directly to `applied`.

### Helm chart values planning

Argo CD inline `helm.valuesObject` and `helm.parameters` are not edited by the current planner, even when the Application is Git-managed. Such layouts fail closed until their effective values are represented by a server-resolved Git-managed values file.

Argo CD local-chart Helm planning first probes Git-managed values paths and accepts an automatically inferred mapping only when rendering proves a unique, exact container-resource change. A chart that supports inference does not need a GreenKube Application annotation. If the chart is ambiguous, the recommendations plan dialog offers a one-time mapping using server-returned Git-managed values-file and container choices plus a resources key path. The API re-resolves the workload binding and render-validates the choice against its repository snapshot; only the file path, container, and key path come from the UI. The mapping is stored with that plan, not written to the binding or Argo CD Application. The annotation remains available as an explicit override:

```yaml
metadata:
  annotations:
    greenkube.cloud/helm-values-mapping: >-
      {"file_path":"apps/payments/values.yaml","container":"api","path":["controller","container","resources"]}
```

For a multi-source Application, include the zero-based `source_index` when using the annotation override for a values source that differs from the chart source. The one-time dialog does not accept source indices or repository identity; it offers only the final Git-managed values file resolved from the server-side binding and snapshot. The file must be one of the resolved Git-managed values files, and `path` must point to the named container's `resources` mapping. GreenKube renders before and after and blocks unrelated changes.

Plans are created against the current immutable head of the resolved source ref, not the revision the GitOps controller last deployed. The plan records the controller-reported revision separately and the preview warns when they differ; approve only after refreshing discovery or syncing the controller if the live workload must match the rendered plan exactly.

Flux HelmRelease planning supports Git-owned release manifests and local charts whose GitRepository source resolves to the same immutable repository snapshot. Release ownership uses Flux's exact default name (`[targetNamespace-]metadata.name`, or just `metadata.name` when `targetNamespace` is omitted); GreenKube does not independently truncate or hash it and blocks plans if the result exceeds Helm's 53-character limit. `chart.spec.valuesFiles` is rendered in order from safe source-relative snapshot paths, and `chartRef` is supported when it references a Git-backed `HelmChart` available in that snapshot. `valuesFrom` ConfigMaps and Secrets are used only when a unique GitRepository-backed Flux Kustomization inventories them in the HelmRelease namespace and same repository/ref/revision. The planner keeps the existing managed post-renderer edit strategy; it does not rewrite values files, ConfigMaps, or Secrets. Remote HelmRepository/OCI chart artifacts, cross-repository/revision values or charts, runtime-only or ambiguous valuesFrom references, `valuesFrom.targetPath`, missing required snapshot files, chart dependencies, unmanaged post-renderers, stale refs, and unrelated rendered changes fail closed with typed unsupported reasons.

#### Isolated renderer (`rendererSandbox`)

When automation is enabled, the chart runs Helm/Kustomize rendering in a separate `renderer-sandbox` Pod. It receives only a bounded repository snapshot; it does not mount provider or database Secrets and does not receive a Kubernetes service-account token. Its root filesystem is read-only and its process/resource/output limits are bounded. The renderer NetworkPolicy denies egress and allows requests only from GreenKube Pods; the cluster CNI must enforce NetworkPolicies before rendering is enabled. For clusters that permit `NET_ADMIN` but do not enforce NetworkPolicy, `rendererSandbox.egressFirewall.enabled` can install IPv4/IPv6 output-deny rules with a one-shot init container; the renderer process itself remains non-root with all capabilities dropped. If the configured boundary cannot be enforced, disable automation-backed rendering; the API/controller fails closed when no isolated renderer is available.

The default snapshot limits are 100 MiB compressed, 500 MiB extracted, 20,000 files, and 2 MiB per file. `rendererSandbox.tmpSizeLimit` bounds the renderer's temporary workspace. The service accepts only the pinned `helm` and `kustomize` commands. Kustomize `helmCharts` remain unsupported because their embedded Helm execution does not yet apply the direct Helm renderer's full safety checks.

Provider snapshots use the GitHub, GitLab, or Gitea archive endpoint pinned to the full commit SHA resolved from the configured ref. The archive body is streamed with the compressed-byte limit before extraction; the extractor normalizes the provider's single repository-root directory and enforces the extracted-byte, file-count, and per-file limits while rejecting unsafe paths and filesystem entries. Archive requests run through the authorized repository-scoped provider, and cross-origin downloads never receive provider authorization headers. Providers that do not support bounded immutable-commit archives fail closed; GreenKube does not retry at the mutable branch name or fall back to per-file reads.

The Helm renderer receives the Kubernetes version and API-version set discovered by the Helm chart at install/upgrade time, so capability-gated templates are rendered against the target cluster. Non-Helm deployments can set `GITOPS_HELM_KUBE_VERSION` and `GITOPS_HELM_API_VERSIONS_JSON` (a JSON array) explicitly.

```yaml
rendererSandbox:
  enabled: true
  replicas: 1
  servicePort: 8090
  tmpSizeLimit: 768Mi
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
    limits:
      cpu: 500m
      memory: 1Gi
```

The local Bubblewrap backend is used only when no remote renderer URL is configured and the runtime kernel permits the required unprivileged namespaces. It fails closed rather than falling back to an unrestricted subprocess.

#### Automation policy (`automation.policy`)

Policy is evaluated on preview/plan creation and revalidated on approval. Risk, confidence, sample/coverage, open-PR budget, cooldown, change-freeze labels, and absolute freeze windows are configurable. Discovery also supplies live workload labels, rollout-in-progress state, controller health/suspend state, and HPA/PDB interaction evidence. A configured freeze label blocks only matching workloads; rollout, controller-health, and HPA/PDB gates reject a plan when their evidence reports a conflict and report `not_available` when discovery cannot collect it. The defaults align with the recommendation sample threshold and require medium-or-lower risk, at least 0.6 confidence, 36 samples, and 50% coverage.

```yaml
automation:
  policy:
    maxRiskLevel: medium
    minConfidence: 0.6
    minSamples: 36
    minCoverage: 0.5
    maxOpenPullRequestsPerWorkload: 1
    cooldownMinutes: 60
    changeFreezeLabels: []
    changeFreezeWindows: []
```

#### Pull request options (`automation.pullRequests`)

```yaml
automation:
  pullRequests:
    draft: false
    labels:
      - greenkube
      - sustainability
    reviewers: []
    teamReviewers: []
```

GitHub supports draft PRs, labels, reviewers, and team reviewers. GitLab supports draft MRs, labels, and numeric reviewer IDs; team reviewers are unsupported. Gitea supports labels and reviewers; draft PRs and team reviewers are unsupported. Unsupported combinations fail before the provider creates a branch or commit.

### Secrets (`secrets`)

| Key | Description |
|-----|-------------|
| `secrets.apiKey` | API bearer token. A random key is generated for the chart-managed Secret when omitted and is preserved across upgrades. An existing Secret must contain `GREENKUBE_API_KEY`; without it, production requests fail closed with `503`. |
| `secrets.electricityMapsToken` | Electricity Maps API token when `config.electricityProvider` is `electricity_maps`. Without it, GreenKube uses bundled zone-specific fallback data when available, then the configured global intensity. Get a token at [electricitymaps.com](https://www.electricitymaps.com/). |
| `secrets.wattnetEmail` | Wattnet account email (only when `config.electricityProvider` is `wattnet`). Register for free at [api.wattnet.eu/token-request/register](https://api.wattnet.eu/token-request/register) |
| `secrets.wattnetPassword` | Wattnet account password (only when `config.electricityProvider` is `wattnet`) |
| `secrets.boaviztaToken` | Optional Boavizta API token. |
| `secrets.existingSecret` | Name of an existing Kubernetes Secret to use instead of creating one from `values.yaml`. This skips chart Secret creation, so include every credential the installation needs (for the bundled database: `POSTGRES_PASSWORD` and `DB_CONNECTION_STRING`). |
| `secrets.dbConnectionString` | PostgreSQL connection string when using an external PostgreSQL instance (`postgres.enabled: false`) with the chart-managed Secret. When `secrets.existingSecret` is set, put `DB_CONNECTION_STRING` in that Secret instead. |
| `secrets.prometheus.username`, `secrets.prometheus.password`, `secrets.prometheus.bearerToken` | Optional Prometheus authentication credentials. |

### Monitoring (`monitoring`)

```yaml
monitoring:
  prometheusRule:
    enabled: false       # Optional automation backlog/stuck-state alerts; requires the PrometheusRule CRD
    namespace: monitoring
    additionalLabels: {}
  serviceMonitor:
    enabled: false       # Set to true if using the Prometheus Operator (kube-prometheus-stack)
    namespace: monitoring
    interval: 30s
  networkPolicy:
    enabled: false       # Restrict selected components to explicit ingress and egress rules
    prometheusNamespace: monitoring
    additionalIngress: []
    additionalEgress: [] # Add narrow rules for external DB, Git provider, or Kubernetes API endpoints.
```

`prometheusRule.enabled` installs alerts for stuck plan operations, plans awaiting controller sync, failed syncs, non-terminal webhook deliveries, and stale worker heartbeats. The corresponding `greenkube_automation_backlog{queue,state}` gauges and heartbeat age are exposed on `/prometheus/metrics`; enable `serviceMonitor` separately to scrape them.

When enabled, the chart applies policies to standalone app/worker pods or production API/controller/worker pods. It allows DNS and the bundled PostgreSQL connection, plus Prometheus and the ready-check ingress paths; other external egress requires an explicit `additionalEgress` rule. No unrestricted internet egress is added.

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

## Production profile settings

The `production` profile adds these values:

```yaml
production:
  migration:
    enabled: true       # Post-install/post-upgrade schema migration Job
    backoffLimit: 6
  leaderElection:
    enabled: true       # Namespaced Lease held by the active controller
    leaseName: ''       # Defaults to <release>-leader
    leaseDurationSeconds: 30
    renewDeadlineSeconds: 10
    retryPeriodSeconds: 2
  components:
    api:
      replicas: 2
    controller:
      replicas: 1
    automation:
      replicas: 1       # Must remain 1; the worker uses database leases
```

API, controller, and worker resources/probes keep their chart defaults unless you override them.

Only the Lease holder runs controller jobs, GitOps discovery, and sync observation. The API ServiceAccount has read-only Kubernetes permissions for health and inventory endpoints. The automation worker must remain at one replica because the database queue provides the execution lease; the chart rejects other replica counts.

For production, create the API key and database credentials in a Kubernetes Secret and set `secrets.existingSecret`. When the bundled PostgreSQL is enabled, that Secret must contain `POSTGRES_PASSWORD` and `DB_CONNECTION_STRING`; when it is disabled, `DB_CONNECTION_STRING` is still required. See the [Installation guide](installation.md#production-api-key-handling).

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

The chart defaults to `environment: production`, disables GitOps automation until explicitly enabled, and creates a random API key in its managed Secret when `secrets.apiKey` is omitted. Existing Secrets must include `GREENKUBE_API_KEY`; otherwise production requests fail closed with `503`. For local development, explicitly set `config.environment: development` and do not expose the service publicly. See the [API authentication notes](api.md#authentication).

The Settings page sends integration changes to `POST /api/v1/config/services`. The API accepts only the current configured service hosts or exact hosts listed in `config.serviceUrlAllowlist`, and does not log the submitted URLs. It updates the running process and attempts to patch the mounted Secret. Check the response's `X-Configuration-Persisted` header; if persistence fails, the change is in memory only and will not survive a restart. Helm remains the recommended source of truth for durable values.

The API expires unapproved GitOps plans once per minute. A daily retention sweep removes rendered file contents and terminal operation/webhook rows older than 90 days while retaining plan metadata, pull-request records, and recommendation audit events. Worker heartbeat rows older than seven days are pruned when the worker starts.

# Configuration Reference

The Helm chart maps `values.yaml` settings to GreenKube's environment-based configuration. The chart's [`values.yaml`](../helm-chart/values.yaml) is the authoritative list of deployment options; this page summarizes the commonly used settings. For a step-by-step installation, see the [Installation guide](installation.md). Data-source URLs and tokens are configuration as code: the Settings page shows them read-only, with the matching Helm snippets.

## Helm values

The chart supports two deployment profiles:

- `profile: standalone` (default) deploys the collector and API together. When automation is enabled, the worker runs inline in the API process.
- `profile: production` renders separate API, controller, and (when automation is enabled) automation worker deployments. The controller uses a Kubernetes Lease for leader election and the migration Job runs before the API is considered ready. The standalone profile does not run a separate worker.

The default values use `profile: standalone`, `config.environment: production`, `config.apiAuthMode: session` (account sign-in, see [Authentication](authentication.md)), and a generated bootstrap API key. The default profile is not a substitute for production authentication or network controls.

### Image

```yaml
image:
  repository: greenkube/greenkube
  tag: 0.3.1
  # Optional: pin the exact image instead of using a tag.
  # digest: sha256:<64 hexadecimal characters>
  pullPolicy: IfNotPresent
```

`image.digest` renders `repository@digest` and takes precedence over `image.tag`. Do not put a `sha256:` digest in `image.tag`; the chart rejects that with an actionable error because Kubernetes would otherwise report `InvalidImageName`.

### General configuration (`config`)

| Key | Default | Description |
|-----|---------|-------------|
| `config.logLevel` | `INFO` | Log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `config.environment` | `production` | Runtime environment. Production never serves anonymous requests. |
| `config.apiAuthMode` | `session` | `API_AUTH_MODE`. `session`: account sign-in with server-side sessions plus scoped API keys; `api_key`: Bearer API keys only; `oidc`: reserved (`503`). See [Authentication](authentication.md). |
| `config.trustedProxyIps` | `""` | `TRUSTED_PROXY_IPS`. Comma-separated IPs/CIDRs of reverse proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` headers are trusted (client IP for rate limits, `Secure` session cookie behind a TLS-terminating Ingress). |
| `config.serviceUrlAllowlist` | `[]` | Exact service hostnames accepted in addition to the configured Prometheus/OpenCost/Boavizta hosts. |
| `config.clusterName` | `""` | Cluster name used as a label in Prometheus metrics |
| `config.cloudProvider` | `unknown` | Cloud provider (`aws`, `gcp`, `azure`, `ovh`, `scaleway`, `on-prem`, `unknown`) |
| `config.defaultZone` | `""` | Fallback Electricity Maps zone code for nodes whose zone or region cannot be mapped (e.g. `FR`, `DE`, `US-CAL-CISO`) |
| `config.defaultIntensity` | `500.0` | Global fallback grid intensity in gCO₂e/kWh when no zone-specific intensity is available |
| `config.intensityMaxAge` | `6h` | `INTENSITY_MAX_AGE`. Maximum age of a stored grid intensity (`<number><ms\|s\|m\|h>`). An older value is stale: the zone default is used instead and rows are tagged `intensity_source=zone_default` (or `stale` when no zone default exists). Defaults are computed at read time and never written to the intensity history. |
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
| `config.api.livenessProbe` | enabled | HTTP liveness probe settings (`path: /api/v1/health/heartbeat`, which answers while the process is responsive and does not depend on the automation loop). |
| `config.api.readinessProbe` | enabled | HTTP readiness probe settings (`path: /api/v1/health`, which returns `503` while a supervised in-process automation loop is down or stale). |
| `config.corsOrigins` | `""` | Comma-separated allowed CORS origins (`CORS_ORIGINS`). Empty (default) keeps the API same-origin. Credentials are allowed only for explicitly listed origins; `*` allows any origin without credentials. |
| `config.apiRateLimit` | `60/minute` | SlowAPI rate limit for API endpoints. |
| `config.rootPath` | `""` | Sub-path prefix when served behind a reverse proxy. |
| `config.metricsListMaxRangeDays` | `30` | Maximum raw `/metrics` listing window; wider ranges use report endpoints. |

### Data retention

| Key | Default | Description |
|-----|---------|-------------|
| `config.metricsCompressionAgeHours` | `24` | Age in hours after which 5-min raw metrics are compressed into hourly aggregates. Only closed hours are compressed, each from its complete raw set, behind a per-table compression watermark. Compressed hours are never re-read, so keep it at `24` or more: the carbon-intensity recompute refreshes only the last 24 hours of raw rows (a lower value logs a warning). |
| `config.metricsRawRetentionDays` | `7` | Days to retain raw metrics. Pruning removes whole hours only, and never rows that are not compressed yet, whatever the retention (even `0`). |
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
| `config.boavizta.url` | `""` | Boavizta API endpoint for embodied emissions. Empty uses the public API `https://api.boavizta.org`, or the URL saved on the Settings page. A URL set here always takes precedence. |
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

### Metric collection (`config.prometheus`, `config.collection`)

The controller collects half-open, step-aligned windows `[start, end)` and records, per source, the end of the last window it fully wrote (the *collection watermark*). Every run collects all missing windows oldest first, so a restart or a Prometheus outage is backfilled automatically instead of leaving a gap.

| Helm value | Environment variable | Default | Description |
|---|---|---|---|
| `config.prometheus.queryRangeStep` | `PROMETHEUS_QUERY_RANGE_STEP` | `5m` | Collection step and stored row duration. Must divide 24 hours. |
| `config.prometheus.scrapeInterval` | `PROMETHEUS_SCRAPE_INTERVAL` | `30s` | cAdvisor scrape interval. Rate windows span `max(step, 4 × scrapeInterval)` so each rate sees at least four scrapes. |
| `config.collection.safetyLag` | `COLLECTION_SAFETY_LAG` | `1m` | A window is collected once its end is at least this old, so late scrapes and remote-write delays are included. Increase it for Thanos/Mimir or agent-mode Prometheus. |
| `config.collection.maxBackfill` | `COLLECTION_MAX_BACKFILL` | `24h` | Catch-up horizon after downtime. Windows older than this are skipped and counted in `greenkube_collection_gap_windows_total`. Keep it within your Prometheus retention. |

Durations accept `s`, `m`, `h` and `d` suffixes. The watermark lag is exported as `greenkube_collection_watermark_lag_seconds{source}`; it stays below two steps in steady state.

### Recommendation engine (`config.recommendations`)

Analyzer thresholds and source selection live under `config.recommendations`. The values are wired to `RECOMMENDATION_*` environment variables. Key entries:

| Helm value | Environment variable | Default | Description |
|---|---|---|---|
| `config.recommendations.vpaEnabled` | `RECOMMENDATION_VPA_ENABLED` | `true` | Read recommendation-mode VPAs (`updateMode: Off`). The source detects the CRD and skips cleanly when absent; native rightsizing for the same workload is then suppressed. |
| `config.recommendations.karpenterEnabled` | `RECOMMENDATION_KARPENTER_ENABLED` | `true` | Karpenter NodePool consolidation recommendations. Auto-detects the CRDs. |
| `config.recommendations.lookbackDays` | `RECOMMENDATION_LOOKBACK_DAYS` | `14` | Lookback of recommendation scans (two weekly cycles). SQLite and PostgreSQL scans read per-target usage summaries from the `workload_usage_hourly` rollup, so memory grows with the number of workloads, not with the lookback. |
| `config.recommendations.maxMetricRows` | `RECOMMENDATION_MAX_METRIC_ROWS` | `750000` | Hard bound on combined and container rows loaded by one recommendation scan on backends without the usage rollup (custom repositories). The lookback window is shortened automatically before loading above this bound. |
| `config.recommendations.rightsizingIncreaseThreshold` | `RIGHTSIZING_INCREASE_THRESHOLD` | `0.9` | Recommend a request increase (reliability) when p99 usage exceeds this share of the request. |
| `config.recommendations.minDecreaseCoverageDays` | `RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS` | `7` | Observed days required before a request decrease is recommended. Increases need no minimum. |
| `config.recommendations.limitsPolicy` | `RECOMMENDATION_LIMITS_POLICY` | `keep` | Limits advice of native rightsizing: `keep` (only when a limit would be invalid or unsafe), `remove_cpu`, or `memory_equals_request`. See [Two-way rightsizing](recommendation.md#two-way-rightsizing-and-limits). |
| `config.recommendations.sourcePriority` | `RECOMMENDATION_SOURCE_PRIORITY` | `vpa,karpenter,greenkube` | Arbitration precedence (highest first). |
| `config.recommendations.ttlDays` | `RECOMMENDATION_TTL_DAYS` | `14` | Recommendation expiry. |
| `config.recommendations.rankingProfile` | `RECOMMENDATION_RANKING_PROFILE` | `balanced` | Default ranking profile: `balanced`, `cost_first`, `carbon_first`, `low_risk` or `quick_wins`, presets over the carbon price, risk aversion and effort weight of the expected value. Requests override it with `?profile=`. See [ranking](recommendation.md#sources-evidence-and-ranking). |
| `config.recommendations.rankingWeights` | `RECOMMENDATION_RANKING_WEIGHTS` | `""` | Deprecated and ignored: weights of the former batch-normalized score. |
| `config.recommendations.incidentCost` | `RECOMMENDATION_INCIDENT_COST` | `100` | Expected cost (projection currency) of a failed high-risk change on one workload of normal criticality. Scaled by risk level, blast radius (scope, `greenkube.io/criticality`, single replica) and the profile's risk aversion. |
| `config.recommendations.engineerHourCost` | `RECOMMENDATION_ENGINEER_HOUR_COST` | `0` | Cost of one engineer hour used to weigh implementation effort. `0` ignores effort, except in the `quick_wins` profile (at least 100 per hour). |
| `config.recommendations.verification.windowHours` | `VERIFICATION_WINDOW_HOURS` | `72` | Fixed post-apply measurement window, starting at rollout completion; the baseline window has the same length and ends at the PR merge. Extended once when trusted samples are missing. |
| `config.recommendations.verification.minSamples` | `VERIFICATION_MIN_SAMPLES` | `36` | Trusted samples (query steps) required in each window; below it after one extension the measurement is `inconclusive`. |
| `config.recommendations.verification.minSavingsRatio` | `VERIFICATION_MIN_SAVINGS_RATIO` | `0.5` | Projection accuracy below which a realized saving is flagged in the measurement notes. Reported only: the projection is not a gate. |
| `config.recommendations.verification.usageHeadroom` | `VERIFICATION_USAGE_HEADROOM` | `1.1` | Per-pod p95 usage ceiling vs the applied per-pod request. |
| `config.recommendations.verification.maxRestartDelta` | `VERIFICATION_MAX_RESTART_DELTA` | `0` | Maximum sum of per-pod restart increases during the window. |
| `config.recommendations.verification.minReadiness` | `VERIFICATION_MIN_READINESS` | `0.99` | Minimum pod readiness over the window: ready samples of `kube_pod_status_ready` (kube-state-metrics) over all samples of the observed pods, so each pod weighs its lifetime. Reported `not_available` when the series is missing. |
| `config.recommendations.verification.maxThrottleRatio` | `VERIFICATION_MAX_THROTTLE_RATIO` | `0.05` | Maximum CPU throttling ratio (`container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total`). Reported `not_available` for containers without a CPU limit. |
| `config.recommendations.verification.lifecycleInterval` | `RECOMMENDATION_LIFECYCLE_INTERVAL` | `5m` | Apply detection / verification / expiry job interval. |
| `config.recommendations.changeMeasurement.enabled` | `CHANGE_MEASUREMENT_ENABLED` | `true` | Record detected changes with a baseline snapshot and measure them against a counterfactual ([method](methodology/savings-measurement.md)). `false` leaves new changes to the interim verifier above. |
| `config.recommendations.changeMeasurement.preWindowDays` | `CHANGE_MEASUREMENT_PRE_WINDOW_DAYS` | `7` | Baseline window, ending at the PR merge or plan approval. Must be at least 7 days so every hour of the week is matched; lower values are rejected at startup. |
| `config.recommendations.changeMeasurement.postWindowDays` | `CHANGE_MEASUREMENT_POST_WINDOW_DAYS` | `7` | Post-change window, starting after the rollout and its warm-up. The outcome is decided once, at its end. |
| `config.recommendations.changeMeasurement.warmupHours` | `CHANGE_MEASUREMENT_WARMUP_HOURS` | `1` | Hours after rollout completion excluded from the post-window. |
| `config.recommendations.changeMeasurement.minCoverage` | `CHANGE_MEASUREMENT_MIN_COVERAGE` | `0.8` | Minimum share of post-window hours matched with a baseline hour of the week (and the minimum coverage of a load driver); below it the measurement is `inconclusive`. |
| `config.recommendations.changeMeasurement.bootstrapSamples` | `CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES` | `1000` | Moving-block bootstrap resamples for the 90% confidence interval (100–100000). |
| `config.recommendations.changeMeasurement.epsilonRatio` | `CHANGE_MEASUREMENT_EPSILON_RATIO` | `0.02` | Noise floor: `not_realized` when the interval's upper bound is at most this share of the counterfactual. |
| `config.recommendations.scanInterval` | `RECOMMENDATION_SCAN_INTERVAL` | `1h` | Interval between scheduled recommendation scans, run by the leader controller only (`1m`–`24h`; units `s`, `m`, `h`). The controller also scans once right after its initial collection. On-demand scans are queued with `POST /api/v1/optimization/runs`. |

### Impact Model (`impact`)

The [Impact Model](methodology/impact-model.md) prices every recommendation projection. A container with an observed OpenCost cost is priced from that cost. Otherwise unit prices come from the first available tier: observed per-resource OpenCost costs on the node, then the bundled list price of the node's instance type and region, then the custom rates below.

| Helm value | Environment variable | Default | Description |
|---|---|---|---|
| `impact.pricing.cpuCoreHour` | `IMPACT_PRICING_CPU_CORE_HOUR` | unset (`0.031611`) | Price of one vCPU-hour for nodes without observed costs or a catalog price. Unset uses the [OpenCost default custom pricing](https://github.com/opencost/opencost/blob/develop/configs/default.json). |
| `impact.pricing.ramGiBHour` | `IMPACT_PRICING_RAM_GIB_HOUR` | unset (`0.004237`) | Price of one GiB-hour of memory, same fallback rules. |
| `impact.pricing.currency` | `IMPACT_PRICING_CURRENCY` | `USD` | ISO 4217 code of all projected costs: the custom rates and observed OpenCost costs (set it to OpenCost's currency). Only bundled catalogs in this currency are used, so set `EUR` to use the OVHcloud and Scaleway catalogs. GreenKube never converts currencies. |
| `impact.realizationFactor` | `IMPACT_REALIZATION_FACTOR` | unset | Override of the share (0–1) of released capacity expected to become removed billable node-hours; `potential_savings_*` equal the allocated projection times this factor. Unset, the controller derives it per node pool from the Cluster Autoscaler status, Karpenter consolidation policies and budgets, and disruption blockers ([RealizationContext](methodology/impact-model.md#realization-factor)); before the first observation it is 1.0. |
| `impact.memoryWattsPerGiB` | `POWER_MEMORY_W_PER_GIB` | `0.392` | Memory power per GiB of node memory (Cloud Carbon Footprint coefficient), allocated to pods by memory reservation. |
| `impact.carbonPricePerTonne` | `CARBON_PRICE_PER_TONNE` | `100` | Currency per tonne CO₂e used to compute `value_annual = expected cost + price × tCO₂e`, the value the expected-value ranking starts from. Set `0` to rank on cost alone; the `carbon_first` profile uses 4 × this price, `cost_first` 0, and `?carbon_price=` overrides it per request. |

On-premises clusters should set both `cpuCoreHour` and `ramGiBHour`; every projection records which tier priced it in `impact.assumptions.unit_prices`.

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
      secretKey: token
      repositoryAllowlist:
        - "acme/gitops"
```

Each provider needs a non-empty repository allowlist; an empty allowlist denies all repositories. Glob matching is segment-aware: `acme/*` matches `acme/repo`, not `acme/team/repo`. Only the provider ID, API/web base URLs, Secret reference, and allowlist are rendered into the ConfigMap. The token stays in the referenced Kubernetes Secret and is mounted read-only at `/etc/greenkube/git-providers/<secret-name>/<secret-key>`; when `secretNamespace` is set, the mount path includes that namespace before the Secret name. The Secret must be in the Helm release namespace. Argo CD and Flux discovery select a configured provider only when its allowlist matches the source repository. Preview and approval remain unavailable for missing credentials, unsupported renderers, or ambiguous sources.

For Gitea, `webBaseUrl` is optional and must be an HTTP(S) origin, such as `https://gitea.example.com` (no repository path or `/api/v1`). Use it when `apiBaseUrl` addresses Gitea through an in-cluster Service but the frontend must link users to the browser-facing Gitea hostname.

The chart adds a checksum of non-secret ConfigMap inputs to each application Pod template. Helm upgrades that change runtime configuration therefore roll the affected Pods; changes to externally managed Secret data still require an explicit restart when the application reads that value at startup.

The nested `tokenSecret: {name, key, namespace}` form and the `repositories` alias are no longer accepted: rendering fails and names the replacement keys (`secretName`, `secretKey`, `secretNamespace`, `repositoryAllowlist`).

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

When automation is enabled, the chart runs Helm/Kustomize rendering in a separate `renderer-sandbox` Pod. It receives only a bounded repository snapshot; it does not mount provider or database Secrets and does not receive a Kubernetes service-account token. Its root filesystem is read-only and its process/resource/output limits are bounded. The renderer NetworkPolicy denies egress and allows requests only from GreenKube Pods; the cluster CNI must enforce NetworkPolicies before rendering is enabled. For clusters that permit `NET_ADMIN` but do not enforce NetworkPolicy, `rendererSandbox.egressFirewall.enabled` can install IPv4/IPv6 output-deny rules with a one-shot init container; the renderer process itself remains non-root with all capabilities dropped. `rendererSandbox.egressFirewall.privilegedInitContainer` is an opt-in local-cluster workaround for runtimes that deny `NET_ADMIN` unless the init container is privileged. Do not enable it in production; use a NetworkPolicy-enforcing CNI instead. If the configured boundary cannot be enforced, disable automation-backed rendering; the API/controller fails closed when no isolated renderer is available.

The default snapshot limits are 100 MiB compressed, 500 MiB extracted, 20,000 files, and 2 MiB per file. `rendererSandbox.tmpSizeLimit` bounds the renderer's temporary workspace. The service accepts only the pinned `helm` and `kustomize` commands. Kustomize `helmCharts` remain unsupported because their embedded Helm execution does not yet apply the direct Helm renderer's full safety checks.

Provider snapshots use the GitHub, GitLab, or Gitea archive endpoint pinned to the full commit SHA resolved from the configured ref. The archive body is streamed with the compressed-byte limit before extraction; the extractor normalizes the provider's single repository-root directory and enforces the extracted-byte, file-count, and per-file limits while rejecting unsafe paths and filesystem entries. Archive requests run through the authorized repository-scoped provider, and cross-origin downloads never receive provider authorization headers. Providers that do not support bounded immutable-commit archives fail closed; GreenKube does not retry at the mutable branch name or fall back to per-file reads.

The Helm renderer receives the Kubernetes version and API-version set discovered by the Helm chart at install/upgrade time, so capability-gated templates are rendered against the target cluster. Non-Helm deployments can set `GITOPS_HELM_KUBE_VERSION` and `GITOPS_HELM_API_VERSIONS_JSON` (a JSON array) explicitly.

```yaml
rendererSandbox:
  enabled: true
  replicas: 1
  servicePort: 8090
  tmpSizeLimit: 768Mi
  egressFirewall:
    enabled: false
    privilegedInitContainer: false
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

Policy is evaluated on preview/plan creation and revalidated on approval. Risk, confidence, sample/coverage, open-PR budget, cooldown, change-freeze labels, and absolute freeze windows are configurable. Discovery also supplies live workload labels, rollout-in-progress state, controller health/suspend state, and HPA, VPA and PDB evidence. A configured freeze label blocks only matching workloads. Rollout, controller-health, HPA, VPA and PDB gates reject a plan when their evidence reports a conflict. An HPA blocks only the resource it scales on, and a VPA blocks only in an active update mode (`Auto`, `Recreate`, `InPlaceOrRecreate`, `InPlace`). See [GitOps automation](automation.md#approval-and-policy). The defaults align with the recommendation sample threshold and require medium-or-lower risk, at least 0.6 confidence, 36 samples, and 50% coverage.

`minCoverage` applies to the evidence attached to each recommendation: the observed CPU or memory samples divided by the expected observation points in that recommendation's window. `minSamples` is the minimum sample count for the recommendation. This is separate from GitOps discovery coverage (`GET /api/v1/gitops/status`), which measures resolved workload bindings. Lowering coverage or sample thresholds can make more PRs eligible but reduces the amount of observability backing each change.

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
    missingEvidence: ''
```

`missingEvidence` sets what the live gates (rollout, controller health, HPA, VPA, PDB) do when discovery could not collect their evidence:

| Value | Behavior |
|---|---|
| `fail` | Reject the plan with `policy_evidence_missing`. |
| `warn` | Allow the plan, mark the gate `not_available`, and add a warning to `policy_result.warnings`. |
| `''` (default) | `fail` for `profile: production`, `warn` for `standalone`. |

Any other value fails `helm template`. Without Helm, an unset value in `GITOPS_POLICY_JSON` means `warn`.

#### Pull request options (`automation.pullRequests`)

```yaml
automation:
  pullRequests:
    draft: false
    labels: []
    reviewers: []
    teamReviewers: []
```

Labels default to none. Configure only labels that already exist in every target repository.

GitHub supports draft PRs, labels, reviewers, and team reviewers. GitLab supports draft MRs, labels, and numeric reviewer IDs; team reviewers are unsupported. Gitea supports labels and reviewers; draft PRs and team reviewers are unsupported. Unsupported combinations fail before the provider creates a branch or commit.

### Secrets (`secrets`)

| Key | Description |
|-----|-------------|
| `secrets.apiKey` | API bearer token, rendered into the Secret `<release>-values`. When omitted, the `<release>-credentials-bootstrap` hook generates one once into the Secret `<release>`; upgrades and GitOps syncs never rotate it. An existing Secret must contain `GREENKUBE_API_KEY`; without it, production requests fail closed with `503`. |
| `secrets.electricityMapsToken` | Electricity Maps API token when `config.electricityProvider` is `electricity_maps`. Without it, GreenKube uses bundled zone-specific fallback data when available, then the configured global intensity. Get a token at [electricitymaps.com](https://www.electricitymaps.com/). |
| `secrets.wattnetEmail` | Wattnet account email (only when `config.electricityProvider` is `wattnet`). Register for free at [api.wattnet.eu/token-request/register](https://api.wattnet.eu/token-request/register) |
| `secrets.wattnetPassword` | Wattnet account password (only when `config.electricityProvider` is `wattnet`) |
| `secrets.boaviztaToken` | Optional Boavizta API token. |
| `secrets.existingSecret` | Name of an existing Kubernetes Secret to use instead of chart-rendered and generated credentials; the GitOps path. The chart renders no Secret and runs no credentials bootstrap hook, so include every credential the installation needs (for the bundled database: `POSTGRES_PASSWORD` and `DB_CONNECTION_STRING`). |
| `secrets.dbConnectionString` | PostgreSQL connection string when using an external PostgreSQL instance (`postgres.enabled: false`) with the chart-managed Secret. When `secrets.existingSecret` is set, put `DB_CONNECTION_STRING` in that Secret instead. |
| `secrets.prometheus.username`, `secrets.prometheus.password`, `secrets.prometheus.bearerToken` | Optional Prometheus authentication credentials. |

Rendering never uses `lookup` or random functions, so `helm template` output is stable under Argo CD and Flux. Values set above go to the Secret `<release>-values`; generated credentials (`GREENKUBE_API_KEY`, and `POSTGRES_PASSWORD` with `DB_CONNECTION_STRING` for the bundled database) are created once by the credentials bootstrap hook in the Secret `<release>`, which is not part of the release and survives `helm uninstall`. See [Credentials and GitOps](installation.md#credentials-and-gitops-argo-cd-flux).

### Controller metrics endpoint (`collector.metricsPort`)

| Key | Default | Description |
|-----|---------|-------------|
| `collector.metricsPort` | `9102` | `CONTROLLER_METRICS_PORT`. Port of the controller's Prometheus endpoint at `/prometheus/metrics` (also served at `/metrics`) (collection source requests/errors, collection run duration, provenance mix of written rows, recommendation scans and optimization runs, verification outcomes, GitOps sync telemetry). Served through the `<release>-controller-metrics` ClusterIP Service and scraped by a dedicated ServiceMonitor when `monitoring.serviceMonitor.enabled` (both are rendered even when `config.api.enabled` is false). `0` disables the listener, the Service and the ServiceMonitor. In the standalone profile the port must differ from `config.api.port` because both containers share the pod network. |

Recommendation scans and verification run in the controller, so their metrics (`greenkube_optimization_runs_total`, `greenkube_last_successful_optimization_run_timestamp_seconds`, `greenkube_verification_outcomes_total`, ...) are served by this endpoint, not by the API. Every controller replica serves its registry, leader or standby.

The chart keeps the API key out of the controller (least privilege), so this endpoint is reachable on the pod network only and serves bounded, non-sensitive counters; enable `monitoring.networkPolicy` to restrict it to Prometheus. When an API key is present in the controller's environment, scrapes must send it as a bearer token.

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

The controller's own metrics endpoint is configured with `collector.metricsPort` (see [Controller metrics endpoint](#controller-metrics-endpoint-collectormetricsport)); `serviceMonitor.enabled` also adds its ServiceMonitor.

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

Only the Lease holder runs controller jobs (including recommendation scans), GitOps discovery, and sync observation. Because scans run in the controller, its default memory limit is 1Gi. The API ServiceAccount has read-only Kubernetes permissions for health and inventory endpoints. The automation worker must remain at one replica because the database queue provides the execution lease; the chart rejects other replica counts.

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

### Authentication (`auth`)

| Key | Default | Setting | Description |
|-----|---------|---------|-------------|
| `auth.admin.username` | `admin` | `AUTH_ADMIN_USERNAME` | Name of the first account, created when no account exists. |
| `auth.admin.existingSecret` | `""` | `AUTH_ADMIN_PASSWORD` | Secret holding the first admin password (key `auth.admin.existingSecretKey`, default `password`), mounted into the API. Empty: the credentials bootstrap hook generates it once into `<release>-initial-admin` (`AUTH_INITIAL_ADMIN_SECRET`) and the UI forces a change at first sign-in. |
| `auth.session.secureCookie` | `auto` | `SESSION_COOKIE_SECURE` | `auto` (HTTPS directly or via `config.trustedProxyIps`), `always`, `never`. `true`/`false` are accepted as `always`/`never`. |
| `auth.session.idleTimeout` | `12h` | `AUTH_SESSION_IDLE_TIMEOUT` | Sliding inactivity timeout (`<n>s\|m\|h\|d`, `m` = minutes). |
| `auth.session.absoluteLifetime` | `7d` | `AUTH_SESSION_ABSOLUTE_LIFETIME` | Maximum session lifetime. |
| `auth.session.rememberLifetime` | `30d` | `AUTH_SESSION_REMEMBER_LIFETIME` | Lifetime with **Keep me signed in**. |
| `auth.login.rateLimit` | `5/minute` | `AUTH_LOGIN_RATE_LIMIT` | Failed sign-ins per username (four times as many per client IP) before an exponential lockout. |
| `monitoring.serviceMonitor.bearerTokenSecret` | `{name: "", key: token}` | | Secret with a `metrics:read` API key for the Prometheus scrape. Empty uses the deprecated bootstrap key. |

`SESSION_COOKIE_NAME` (default `greenkube_session`) is not exposed in values.

The chart defaults to `environment: production`, disables GitOps automation until explicitly enabled, and generates a random bootstrap API key once (credentials bootstrap hook, Secret `<release>`) when `secrets.apiKey` is omitted. That key is deprecated: people sign in with accounts and machines use scoped API keys. For local development, `config.environment: development` leaves an instance without any account and without a bootstrap key open; do not expose it publicly. See [Authentication](authentication.md).

### Data sources are configuration as code

Data-source endpoints and tokens (Prometheus, OpenCost, Boavizta, Electricity Maps, Wattnet) are set in Helm values or Secrets only (roadmap A13). The Settings page and the connectivity popup show them read-only with copy-paste Helm snippets; `POST /api/v1/config/services` answers `410 Gone`. Runtime edits reached only the API process while the controller kept its collectors (BUG-11), and writing them into the release Secret drifted from Git (BUG-60); the API no longer has any RBAC to write that Secret.

With the chart, Pods read each service URL and token from the first of these that holds it:

1. The Secret `<release>-values`, when you set the key in values (`config.prometheus.url`, `config.opencost.url`, `config.boavizta.url`, `secrets.*`).
2. The Secret `<release>`, which may still hold overrides saved by the Settings page of an earlier release.
3. The built-in default or in-cluster discovery.

To change a data source, set it in values (or in your `secrets.existingSecret`) and upgrade. To drop an old Settings-page override from the Secret `<release>`, remove its key, for example `kubectl -n greenkube patch secret greenkube --type=json -p='[{"op":"remove","path":"/data/PROMETHEUS_URL"}]'`.

The API expires unapproved GitOps plans once per minute. A daily retention sweep removes rendered file contents and terminal operation/webhook rows older than 90 days while retaining plan metadata, pull-request records, and recommendation audit events. Worker heartbeat rows older than seven days are pruned when the worker starts.

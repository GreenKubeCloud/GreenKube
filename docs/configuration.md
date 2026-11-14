# Configuration Reference

The Helm chart maps `values.yaml` settings to GreenKube's environment-based configuration. The chart's [`values.yaml`](../helm-chart/values.yaml) is the authoritative list of deployment options; this page summarizes the commonly used settings. For a step-by-step installation, see the [Installation guide](installation.md). Data-source URLs and tokens are configuration as code: the Settings page shows them read-only, with the matching Helm snippets.

## Helm values

One install topology (roadmap F11): the chart always deploys `<release>-api`, `<release>-controller`,
`<release>-renderer` and the bundled PostgreSQL, plus an optional `<release>-worker`. There is no profile and no
automation feature flag. The chart ships a `values.schema.json` that rejects wrong types and out-of-range values
before rendering. Values of 0.3.x (`profile`, `replicaCount`, `production.*`, `automation.enabled`,
`automation.mode`, `rendererSandbox.*`, `collector.*`, SQLite) are mapped for one minor release and listed in the
install notes; see [Upgrading](upgrading.md).

### Components (`api`, `controller`, `renderer`, `worker`)

| Key | Default | Description |
|-----|---------|-------------|
| `api.replicas` | `1` | Stateless API, UI, webhooks and sign-in; scale freely (sessions live in PostgreSQL). |
| `api.resources` | 100m / 128Mi requests, 500m / 512Mi limits | API container resources. |
| `api.livenessProbe` | `/api/v1/health/heartbeat` | Process liveness only; never restarts the API for a slow dependency. |
| `api.readinessProbe` | `/api/v1/health` | `503 migrating` until the controller has applied every migration, then `503 degraded` only for a wedged in-process loop. |
| `controller.replicas` | `1` | Leader election is always on; extra replicas are hot standbys. `0` only while migrating a SQLite install. |
| `controller.resources` | 100m / 256Mi requests, 1 CPU / 1Gi limits | Runs collection, scans, measurement and automation; memory grows with the number of pods. |
| `controller.metricsPort` | `9102` | Port of `/prometheus/metrics`, `/healthz` (liveness) and `/readyz` (loop registry readiness). `0` disables the listener; the probes then fall back to a process check. |
| `controller.leaderElection.leaseName` | `""` | Lease name; empty uses `<release>-leader`. The Lease is always rendered. |
| `controller.leaderElection.leaseDurationSeconds` | `30` | Handover time when the leader dies. |
| `controller.startupProbe` | 60 x 10 s | Startup budget. `/healthz` answers while the migrations run (`DB_RUN_MIGRATIONS=true` in the controller only), so a long migration is not restarted; `/readyz` stays not-ready until the loops start. |
| `renderer.replicas` | `1` | Isolated Helm/Kustomize renderer, always deployed. |
| `renderer.resources` | 25m / 96Mi requests, 500m / 1Gi limits | Renderer resources. |
| `renderer.servicePort`, `renderer.tmpSizeLimit` | `8090`, `768Mi` | Renderer port and workspace size. |
| `renderer.egressFirewall.enabled` | `false` | Init-container egress firewall (needs `NET_ADMIN`) for CNIs that do not enforce NetworkPolicy. |
| `worker.replicas` | `0` | Optional dedicated plan executors for very large installations. |
| `worker.metricsPort`, `worker.resources` | `8001`, 50m / 64Mi | Worker metrics port and resources. |
| `tmp.sizeLimit` | `768Mi` | Scratch space of the api, controller and worker. |
| `initContainer.image` | `busybox:1.37@sha256:a7dbc319…` | Image of the `wait-for-postgres` init container (bundled PostgreSQL only) and of the Helm test pod, pinned by digest. |

Default requests total about 325m CPU and 600 MiB with PostgreSQL. High availability is `api.replicas: 2` and
`controller.replicas: 2`.

### Scheduling and availability

Each workload has its own block: `api`, `controller`, `worker`, `renderer`, `postgres` and `postgres.backup`.

| Key (per block) | Default | Description |
|-----|---------|-------------|
| `nodeSelector` | `{}` | Pod `nodeSelector`. |
| `tolerations` | `[]` | Pod tolerations. |
| `affinity` | `{}` | Pod affinity; when set, it replaces the default anti-affinity. |
| `podAntiAffinity` | `soft` | `api`, `controller`, `worker`, `renderer` only: with more than one replica and no `affinity`, `soft` prefers one replica per node (`kubernetes.io/hostname`), `hard` requires it, `none` renders nothing. |
| `topologySpreadConstraints` | `[]` | Pod topology spread constraints (not on `postgres.backup`). |
| `priorityClassName` | `""` | Pod priority class. |
| `podAnnotations` | `{}` | Added to the pod template (the config checksum annotation is kept). |
| `podLabels` | `{}` | Added to the pod template; the selector labels always win. |

| Key | Default | Description |
|-----|---------|-------------|
| `global.imagePullSecrets` | `[]` | Pull secrets of every pod the chart renders except the credentials bootstrap Job: names (`[regcred]`) or entries (`[{name: regcred}]`). |
| `api.podDisruptionBudget`, `controller.podDisruptionBudget` | `{enabled: true, minAvailable: 1}` | Rendered only with more than one replica (a budget on a single replica would block node drains). |
| `controller.strategy`, `worker.strategy` | `{type: Recreate}` | The old release stops before the new controller migrates the schema, so two versions never write side by side. `RollingUpdate` (e.g. `maxSurge: 0`) trades that guarantee for a shorter gap. |
| `postgres.shmSizeLimit` | `128Mi` | Memory-backed `/dev/shm` for parallel query workers (the runtime default is 64Mi); counts against the memory limit when used. `""` keeps the runtime default. |
| `postgres.terminationGracePeriodSeconds` | `60` | Time for a clean shutdown checkpoint. |
| `postgres.backup.enabled` | `false` | Daily `pg_dump` CronJob `<release>-postgres-backup` into the claim `<release>-postgres-backup` ([Backup and restore](operations/backup-restore.md)). |
| `postgres.backup.schedule` | `17 3 * * *` | Cron schedule (cluster time zone). |
| `postgres.backup.keep` | `7` | Dumps kept on the volume; older ones are deleted after each successful run. |
| `postgres.backup.activeDeadlineSeconds` | `3600` | Deadline of one run. |
| `postgres.backup.persistence.existingClaim` | `""` | Write to this claim instead of creating `<release>-postgres-backup` (kept by `helm uninstall`). |
| `postgres.backup.persistence.size`, `.storageClassName` | `20Gi`, `""` | Size and class of the created claim. |
| `postgres.backup.resources` | 50m / 64Mi requests, 500m / 512Mi limits | Backup container resources. |

Helper images are pinned by their multi-arch index digest in the default tag (`postgres.image.tag:
18-alpine@sha256:…`, `initContainer.image.tag: 1.37@sha256:…`), so a re-pushed tag cannot change what runs.
Overriding the tag replaces the pin; set `digest: sha256:<64 hex>` next to a plain tag to pin your own.

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
| `config.logFormat` | `json` | `LOG_FORMAT`: `json` (structured, for Loki/Grafana) or `console` (human-readable). |
| `config.environment` | `production` | Runtime environment. Production never serves anonymous requests. |
| `config.apiAuthMode` | `session` | `API_AUTH_MODE`. `session`: account sign-in with server-side sessions plus scoped API keys; `api_key`: Bearer API keys only; `proxy`: single sign-on through an authentication proxy whose identity headers are trusted only from `config.trustedProxyIps` (required); `oidc`: native OpenID Connect sign-in (`auth.oidc.*`). See [Authentication](authentication.md). |
| `config.trustedProxyIps` | `""` | `TRUSTED_PROXY_IPS`. Comma-separated IPs/CIDRs of reverse proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` headers are trusted (client IP for rate limits, `Secure` session cookie behind a TLS-terminating Ingress) and, with `apiAuthMode: proxy`, the only peers whose identity headers are accepted. Behind an Ingress or a Gateway, list the controller or gateway Pods (often the pod CIDR, e.g. `10.244.0.0/16`): otherwise all clients share the proxy's address for rate limits and sign-in lockouts, and `helm install` prints a warning. |
| `config.externalUrl` | `""` | `GREENKUBE_EXTERNAL_URL`. Public URL of the GreenKube UI (http(s), no query or fragment), used for deep links in pull requests, commit statuses and status comments. Empty: no links. |
| `config.clusterName` | `""` | Cluster name used as a label in Prometheus metrics |
| `config.cloudProvider` | `unknown` | Cloud provider (`aws`, `gcp`, `azure`, `ovh`, `scaleway`, `on-prem`, `unknown`) |
| `config.defaultZone` | `""` | Fallback Electricity Maps zone code for nodes whose zone or region cannot be mapped (e.g. `FR`, `DE`, `US-CAL-CISO`) |
| `config.defaultIntensity` | `500.0` | Global fallback grid intensity in gCO₂e/kWh when no zone-specific intensity is available |
| `config.defaults.pue` | `1.3` | `DEFAULT_PUE`: Power Usage Effectiveness used when the cloud provider has no specific value. |
| `config.defaults.instance.vcores` | `1` | `DEFAULT_INSTANCE_VCORES`: vCPUs of the fallback power profile of an unknown instance type. |
| `config.defaults.instance.minWatts` | `1.0` | `DEFAULT_INSTANCE_MIN_WATTS`: idle power (watts) of the fallback profile for `vcores` vCPUs, scaled to the node core count. |
| `config.defaults.instance.maxWatts` | `10.0` | `DEFAULT_INSTANCE_MAX_WATTS`: full-load power (watts) of the fallback profile for `vcores` vCPUs, scaled likewise. |
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
| `config.api.host` | `0.0.0.0` | API bind address inside the container. |
| `config.api.port` | `8000` | API/UI container and Service port. |
| `config.corsOrigins` | `""` | Comma-separated allowed CORS origins (`CORS_ORIGINS`). Empty (default) keeps the API same-origin. Credentials are allowed only for explicitly listed origins; `*` allows any origin without credentials. |
| `config.apiRateLimit` | `60/minute` | `API_RATE_LIMIT`. Rate limit per client IP and per route template (`/api/v1/automation/plans/{plan_id}` is one route, whatever the id); several limits can be combined with `;`. The client IP is the rightmost `X-Forwarded-For` hop that is not in `config.trustedProxyIps`. |
| `config.rootPath` | `""` | Sub-path prefix when served behind a reverse proxy. |
| `config.metricsListMaxRangeDays` | `30` | Maximum raw `/metrics` listing window; wider ranges use report endpoints. |

The API is always deployed (it serves the UI). The 0.3.x keys `config.api.enabled`, `config.api.livenessProbe` and
`config.api.readinessProbe` are deprecated: they are mapped to `api.*` for one minor release
([Upgrading](upgrading.md)).

### Data retention

| Key | Default | Description |
|-----|---------|-------------|
| `config.metricsCompressionAgeHours` | `24` | Age in hours after which 5-min raw metrics are compressed into hourly aggregates. Only closed hours are compressed, each from its complete raw set, behind a per-table compression watermark. Compressed hours are never re-read, so keep it at `24` or more: the carbon-intensity recompute refreshes only the last 24 hours of raw rows (a lower value logs a warning). |
| `config.metricsRawRetentionDays` | `7` | Days to retain raw metrics. Pruning removes whole hours only, and never rows that are not compressed yet, whatever the retention (even `0`). The `__idle__` rows of `node_idle_metrics` are pruned with the pod rows (same retention and compression watermark). |
| `config.metricsAggregatedRetentionDays` | `-1` | Days to retain hourly aggregates. `-1` means indefinite (useful for multi-year trend analysis) |

### Database (`config.db`)

| Key | Default | Description |
|-----|---------|-------------|
| `config.db.schema` | `public` | PostgreSQL schema. |
| `config.db.sslMode` | `disable` | PostgreSQL SSL mode (`disable`, `require`, `verify-full`, ...). |
| `config.db.poolMinSize` | `1` | PostgreSQL connection-pool minimum. |
| `config.db.poolMaxSize` | `10` | PostgreSQL connection-pool maximum. |
| `config.db.statementTimeoutMs` | `30000` | Per-statement PostgreSQL timeout. |

The chart always uses PostgreSQL (bundled, or external with `postgres.enabled: false`). Only the controller
migrates the schema, under a PostgreSQL advisory lock (`DB_RUN_MIGRATIONS`); the API and the workers wait for it.
SQLite (`DB_TYPE=sqlite`, `DB_PATH`) remains for `greenkube demo` and local development. A 0.3.x release with
`config.db.type: sqlite` fails to upgrade with the migration commands ([Upgrading](upgrading.md#from-a-sqlite-install));
`config.persistence.*` keeps the former SQLite claim (never deleted) until you remove it.

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

| Helm value | Environment variable | Default | Description |
|---|---|---|---|
| `config.prometheus.verifyCerts` | `PROMETHEUS_VERIFY_CERTS` | `true` | Verify the TLS certificate of Prometheus. |
| `config.prometheus.nodeInstanceLabel` | `PROMETHEUS_NODE_INSTANCE_LABEL` | `label_node_kubernetes_io_instance_type` | kube-state-metrics node label that holds the instance type. |
| `config.opencost.verifyCerts` | `OPENCOST_VERIFY_CERTS` | `true` | Verify the TLS certificate of OpenCost. |

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
| `config.recommendations.minSamples` | `RECOMMENDATION_MIN_SAMPLES`, `VERIFICATION_MIN_SAMPLES`, policy `minSamples` | `36` | Raw samples of evidence required everywhere a sample count gates a decision: recommendation confidence, the plan policy gate and verification (one knob since 0.4). The deprecated `automation.policy.minSamples` and `config.recommendations.verification.minSamples` still override their gate for one minor release. |
| `config.recommendations.lookbackDays` | `RECOMMENDATION_LOOKBACK_DAYS` | `14` | Lookback of recommendation scans (two weekly cycles). SQLite and PostgreSQL scans read per-target usage summaries from the `workload_usage_hourly` rollup, so memory grows with the number of workloads, not with the lookback. |
| `config.recommendations.maxMetricRows` | `RECOMMENDATION_MAX_METRIC_ROWS` | `750000` | Hard bound on combined and container rows loaded by one recommendation scan on backends without the usage rollup (custom repositories). The lookback window is shortened automatically before loading above this bound. |
| `config.recommendations.rightsizingCpuThreshold` | `RIGHTSIZING_CPU_THRESHOLD` | `0.3` | A CPU request decrease is considered when average CPU usage divided by the request is below this ratio. |
| `config.recommendations.rightsizingMemoryThreshold` | `RIGHTSIZING_MEMORY_THRESHOLD` | `0.3` | Same ratio for memory requests. |
| `config.recommendations.rightsizingHeadroom` | `RIGHTSIZING_HEADROOM` | `1.2` | Safety multiplier applied to the sizing percentile (p95 for decreases, p99 for increases) to get the target request. |
| `config.recommendations.minCpuMillicores` | `RECOMMENDATION_MIN_CPU_MILLICORES` | `10` | Floor of a proposed CPU request; lower targets are raised to it. |
| `config.recommendations.minMemoryBytes` | `RECOMMENDATION_MIN_MEMORY_BYTES` | `16777216` | Floor of a proposed memory request (16 MiB; quote it in values so YAML keeps an integer). |
| `config.recommendations.applyTolerance` | `RECOMMENDATION_APPLY_TOLERANCE` | `0.25` | Relative deviation between the live request and the proposal still counted as applied by apply detection ([Recommendation lifecycle](recommendation.md)). |
| `config.recommendations.zombieCostThreshold` | `ZOMBIE_COST_THRESHOLD` | `0.01` | A workload whose cost over the scan lookback exceeds this (pricing currency) while its energy stays below `zombieEnergyThreshold` is a zombie candidate. |
| `config.recommendations.zombieEnergyThreshold` | `ZOMBIE_ENERGY_THRESHOLD` | `1000` | Energy (joules over the scan lookback) below which a costly workload is a zombie candidate. |
| `config.recommendations.autoscalingCvThreshold` | `AUTOSCALING_CV_THRESHOLD` | `0.7` | Coefficient of variation of CPU usage above which a workload without an HPA is an autoscaling candidate. |
| `config.recommendations.autoscalingSpikeRatio` | `AUTOSCALING_SPIKE_RATIO` | `3.0` | Max/mean CPU ratio required, with the coefficient of variation, for an autoscaling candidate. |
| `config.recommendations.offPeakIdleThreshold` | `OFF_PEAK_IDLE_THRESHOLD` | `0.05` | Share of the daily CPU peak below which an hour counts as idle for off-peak scaling. |
| `config.recommendations.offPeakMinIdleHours` | `OFF_PEAK_MIN_IDLE_HOURS` | `4` | Consecutive idle hours required for an off-peak scale-to-zero recommendation. |
| `config.recommendations.idleNamespaceEnergyThreshold` | `IDLE_NAMESPACE_ENERGY_THRESHOLD` | `1000` | Namespace energy (joules) below which a namespace with a positive cost is idle. |
| `config.recommendations.carbonAwareThreshold` | `CARBON_AWARE_THRESHOLD` | `1.5` | A workload whose average grid intensity exceeds this multiple of its zone average gets a carbon-aware scheduling recommendation. |
| `config.recommendations.nodeUtilizationThreshold` | `NODE_UTILIZATION_THRESHOLD` | `0.2` | Average CPU (and memory, when known) utilization below which a node is over-provisioned. |
| `config.recommendations.recommendSystemNamespaces` | `RECOMMEND_SYSTEM_NAMESPACES` | `false` | Include system namespaces (such as `kube-system`) in recommendations and the sustainability score. Zombie, idle-namespace and off-peak recommendations never target infrastructure namespaces, whatever this says. |
| `config.recommendations.storageCostPerGibMonth` | `STORAGE_COST_PER_GIB_MONTH` | `0.1` | Assumed monthly cost per GiB of an orphaned PersistentVolume, for its deletion savings. |
| `config.recommendations.loadBalancerCostPerMonth` | `LOAD_BALANCER_COST_PER_MONTH` | `18.0` | Assumed monthly cost of an orphaned cloud LoadBalancer, for its deletion savings. |
| `config.recommendations.rightsizingIncreaseThreshold` | `RIGHTSIZING_INCREASE_THRESHOLD` | `0.9` | Recommend a request increase (reliability) when p99 usage exceeds this share of the request. |
| `config.recommendations.rightsizingMaxIncreaseFactor` | `RIGHTSIZING_MAX_INCREASE_FACTOR` | `2.0` | Cap of one request increase step, as a multiple of the current request (must be above 1). OOM kills lift the cap. The reason states the cap and the uncapped target; the next scan re-evaluates. See [How GreenKube sizes increases](recommendation.md#how-greenkube-sizes-increases). |
| `config.recommendations.rightsizingWarmupMinutes` | `RIGHTSIZING_WARMUP_MINUTES` | `10` | Minutes after a container restart (or a new replica) left out of usage percentiles, so startup spikes do not size requests. The maximum keeps every point. `0` disables it. |
| `config.recommendations.minDecreaseCoverageDays` | `RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS` | `7` | Observed days required before a request decrease is recommended. Increases need no minimum. |
| `config.recommendations.deletionMinUnusedDays` | `DELETION_MIN_UNUSED_DAYS` | `7` | Days a LoadBalancer Service or PersistentVolume must look unused continuously, and days of idle observations for zombie workloads and idle namespaces, before a deletion is recommended. See [Why GreenKube thinks a resource is unused](recommendation.md#why-greenkube-thinks-a-resource-is-unused). |
| `config.recommendations.limitsPolicy` | `RECOMMENDATION_LIMITS_POLICY` | `keep` | Limits advice of native rightsizing: `keep` (only when a limit would be invalid or unsafe), `remove_cpu`, or `memory_equals_request`. See [Two-way rightsizing](recommendation.md#two-way-rightsizing-and-limits). |
| `config.recommendations.consensusPolicy` | `RECOMMENDATION_CONSENSUS_POLICY` | `safest` | How the proposals of several sources on one target are combined: `safest` (least aggressive value), `median`, `weighted` (median weighted by each source's measured track record) or `priority` (the highest `sources.priority` source wins, the behavior before consensus). Sources sharing an engine (Goldilocks and VPA) count once. See [Consensus across sources](recommendation.md#consensus-across-sources). |
| `config.recommendations.ttlDays` | `RECOMMENDATION_TTL_DAYS` | `14` | Recommendation expiry. |
| `config.recommendations.rankingProfile` | `RECOMMENDATION_RANKING_PROFILE` | `balanced` | Default ranking profile: `balanced`, `cost_first`, `carbon_first`, `low_risk` or `quick_wins`, presets over the carbon price, risk aversion and effort weight of the expected value. Requests override it with `?profile=`. See [ranking](recommendation.md#sources-evidence-and-ranking). |
| `config.recommendations.rankingWeights` | `RECOMMENDATION_RANKING_WEIGHTS` | `""` | Deprecated and ignored: weights of the former batch-normalized score. |
| `config.recommendations.incidentCost` | `RECOMMENDATION_INCIDENT_COST` | `100` | Expected cost (projection currency) of a failed high-risk change on one workload of normal criticality. Scaled by risk level, blast radius (scope, `greenkube.io/criticality`, single replica) and the profile's risk aversion. |
| `config.recommendations.engineerHourCost` | `RECOMMENDATION_ENGINEER_HOUR_COST` | `0` | Cost of one engineer hour used to weigh implementation effort. `0` ignores effort, except in the `quick_wins` profile (at least 100 per hour). |
| `config.recommendations.verification.windowHours` | `VERIFICATION_WINDOW_HOURS` | `72` | Fixed post-apply measurement window, starting at rollout completion; the baseline window has the same length and ends at the PR merge. Extended once when trusted samples are missing. |
| `config.recommendations.verification.minSavingsRatio` | `VERIFICATION_MIN_SAVINGS_RATIO` | `0.5` | Projection accuracy below which a realized saving is flagged in the measurement notes. Reported only: the projection is not a gate. |
| `config.recommendations.verification.usageHeadroom` | `VERIFICATION_USAGE_HEADROOM` | `1.1` | Per-pod p95 usage ceiling vs the applied per-pod request. |
| `config.recommendations.verification.maxRestartDelta` | `VERIFICATION_MAX_RESTART_DELTA` | `0` | Maximum sum of per-pod restart increases during the window. |
| `config.recommendations.verification.minReadiness` | `VERIFICATION_MIN_READINESS` | `0.99` | Minimum pod readiness over the window: ready samples of `kube_pod_status_ready` (kube-state-metrics) over all samples of the observed pods, so each pod weighs its lifetime. Reported `not_available` when the series is missing. |
| `config.recommendations.verification.maxThrottleRatio` | `VERIFICATION_MAX_THROTTLE_RATIO` | `0.05` | Maximum CPU throttling ratio (`container_cpu_cfs_throttled_periods_total / container_cpu_cfs_periods_total`). Reported `not_available` for containers without a CPU limit. |
| `config.recommendations.verification.lifecycleInterval` | `RECOMMENDATION_LIFECYCLE_INTERVAL` | `5m` | Apply detection / verification / expiry job interval. |
| `config.recommendations.changeMeasurement.enabled` | `CHANGE_MEASUREMENT_ENABLED` | `true` | Record detected changes with a baseline snapshot and measure them against a counterfactual ([method](methodology/savings-measurement.md)). `false` leaves new changes to the interim verifier above. |
| `config.recommendations.changeMeasurement.preWindowDays` | `CHANGE_MEASUREMENT_PRE_WINDOW_DAYS` | `7` | Baseline window, ending at the PR merge or plan approval. Must be at least 7 days so every hour of the week is matched; lower values are rejected at startup and by the values schema, unless `accelerated` is `true` (then at least 1 hour). |
| `config.recommendations.changeMeasurement.postWindowDays` | `CHANGE_MEASUREMENT_POST_WINDOW_DAYS` | `7` | Post-change window, starting after the rollout and its warm-up. The outcome is decided once, at its end. |
| `config.recommendations.changeMeasurement.warmupHours` | `CHANGE_MEASUREMENT_WARMUP_HOURS` | `1` | Hours after rollout completion excluded from the post-window. |
| `config.recommendations.changeMeasurement.minCoverage` | `CHANGE_MEASUREMENT_MIN_COVERAGE` | `0.8` | Minimum share of post-window hours matched with a baseline hour of the week (and the minimum coverage of a load driver); below it the measurement is `inconclusive`. |
| `config.recommendations.changeMeasurement.bootstrapSamples` | `CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES` | `1000` | Moving-block bootstrap resamples for the 90% confidence interval (100–100000). |
| `config.recommendations.changeMeasurement.epsilonRatio` | `CHANGE_MEASUREMENT_EPSILON_RATIO` | `0.02` | Noise floor: `not_realized` when the interval's upper bound is at most this share of the counterfactual. |
| `config.recommendations.changeMeasurement.driftThreshold` | `CHANGE_DRIFT_THRESHOLD` | `0.5` | Drift monitor: share of a realized change's per-replica saving that must be lost for a day to count as drifted ([method](methodology/savings-measurement.md#10-drift-monitor-c8)). |
| `config.recommendations.changeMeasurement.driftSustainedDays` | `CHANGE_DRIFT_SUSTAINED_DAYS` | `3` | Consecutive drifted days (1 to 7) after which the change is `regressed` and its accruals since the drift began are reversed. Both drift settings are read by the controller's `savings-drift` loop; the values schema rejects a threshold outside (0, 1] and a day count outside 1 to 7. |
| `config.recommendations.changeMeasurement.accelerated` | `CHANGE_MEASUREMENT_ACCELERATED` | `false` | Demo and quickstart only: lifts the 7-day minimum of `preWindowDays` down to 1 hour so a change is decided minutes after it lands (`docs/examples/quickstart/values-change-measurement.yaml`). Short windows match few or no hours of the week, so such measurements are mostly `inconclusive`; the controller logs a warning at startup. Keep `false` in production. |
| `config.ledger.chainVerifyInterval` | `LEDGER_CHAIN_VERIFY_INTERVAL` | `1h` | Interval of the controller's `ledger-chain` loop (1m to 24h): catches the append-only savings ledger v2 up with the accrual path (a full anti-join sweep of the v1 ledger, supersessions, restatements) and verifies each cluster's hash chain; failures increment `greenkube_ledger_chain_verification_failures_total` ([ledger](methodology/savings-measurement.md#12-savings-ledger-v2-c6)). |
| `config.recommendations.scanInterval` | `RECOMMENDATION_SCAN_INTERVAL` | `1h` | Interval between scheduled recommendation scans, run by the leader controller only (`1m`–`24h`; units `s`, `m`, `h`). The controller also scans once right after its initial collection. On-demand scans are queued with `POST /api/v1/optimization/runs`. |

### Recommendation sources (`sources`)

Sources produce the signals recommendations are built from: one per native analyzer (`greenkube.rightsizing_cpu`, `greenkube.orphaned_lb`...), `vpa` (with Goldilocks provenance), `greenkube.nodepool`, the signal-only connectors `krr`, `kubecost` and `push`, and third-party plugins. `GET /api/v1/sources` lists them with their status. See [Recommendation sources](sources.md).

| Helm value | Environment variable | Default | Description |
|---|---|---|---|
| `sources.pluginsEnabled` | `SOURCES_PLUGINS_ENABLED` | `true` | Load third-party sources installed in the image and registered under the `greenkube.sources` Python entry points. A plugin that fails to load is reported in `GET /api/v1/sources` (`problems`) and never stops the other sources. |
| `sources.settings` | `SOURCES_CONFIG` | `{}` | Per-source settings keyed by source id, rendered as a JSON object. `enabled` is `true`, `false` or `auto` (run when the source's prerequisites are detected, the default for `vpa` and `greenkube.nodepool`); other keys are validated by the source (unknown keys disable that source and are reported). Invalid JSON is rejected at startup. Do not put credentials here: the map is stored in the ConfigMap. The 0.3.x `config.recommendations.vpaEnabled`/`karpenterEnabled` flags are still honoured for one minor release. |
| `sources.priority` | `RECOMMENDATION_SOURCE_PRIORITY` | `vpa,karpenter,greenkube` | Arbitration precedence (highest first). Replaces `config.recommendations.sourcePriority`. |

| `sources.krr.job.enabled` | — | `false` | Run [Robusta KRR](sources/krr.md) as a CronJob that pushes its report to `POST /api/v1/sources/krr/reports`. Needs `sources.krr.job.apiKeySecret.name` (a Secret holding an API key with scope `signals:report`; `signals:write` is accepted, deprecated). The API is always deployed. |
| `sources.krr.job.schedule` | — | `17 3 * * *` | Cron schedule of the KRR job. |
| `sources.krr.job.strategy` | — | `simple` | KRR strategy (`krr <strategy>`). |
| `sources.krr.job.image.*` | — | `robustadev/krr` v1.31.1 by digest | KRR image; `digest` is required (`sha256:<64 hex>`). |
| `sources.krr.job.prometheusUrl` | — | `""` | Prometheus URL passed to KRR (`-p`); empty lets KRR discover it. |
| `sources.krr.job.extraArgs` | — | `[]` | Extra KRR arguments (`--namespace`, `--history_duration`...). |
| `sources.krr.job.apiKeySecret` | — | `{name: "", key: api-key}` | Secret and key of the `signals:report` API key. |
| `sources.krr.job.resources`, `activeDeadlineSeconds` | — | `100m`/`512Mi` requests, `2Gi` limit; `3600` | KRR container resources and job deadline. |
| `sources.polaris.job.enabled` | — | `false` | Run [Fairwinds Polaris](sources/polaris.md) as a CronJob that pushes its audit to `POST /api/v1/sources/polaris/reports`. Needs `sources.polaris.job.apiKeySecret.name` (a Secret holding an API key with scope `signals:report`; `signals:write` is accepted, deprecated). The API is always deployed. |
| `sources.polaris.job.schedule` | — | `37 3 * * *` | Cron schedule of the Polaris job. |
| `sources.polaris.job.image.*` | — | `quay.io/fairwinds/polaris` 10.1.8 by digest | Polaris image; `digest` is required (`sha256:<64 hex>`). |
| `sources.polaris.job.extraArgs` | — | `[]` | Extra `polaris audit` arguments. |
| `sources.polaris.job.complete` | — | `true` | `true`: each audit replaces every earlier Polaris finding. Set `false` when `extraArgs` narrows the audit (for example one namespace): the job posts with `?complete=false` and adds or refreshes findings. |
| `sources.polaris.job.apiKeySecret` | — | `{name: "", key: api-key}` | Secret and key of the `signals:report` API key. |
| `sources.polaris.job.resources`, `activeDeadlineSeconds` | — | `50m`/`128Mi` requests, `512Mi` limit; `1800` | Polaris container resources and job deadline. |
| `sources.polaris.job.rbac.create`, `serviceAccountName` | — | `true`, `""` | Create the job's read-only ServiceAccount and ClusterRole, or bring your own. |
| `sources.krr.job.rbac.create`, `serviceAccountName` | — | `true`, `""` | Create the job's ServiceAccount and read-only ClusterRole, or use an existing ServiceAccount. |

Connector settings in `sources.settings` (all optional, validated by each source):

| Source | Keys |
|---|---|
| `krr`, `push` | `max_age_hours` (default `168`): pushed signals older than this are no longer used. |
| `kubecost` | `url` (default: the chart service `kubecost-cost-analyzer.kubecost`, probed in-cluster only), `version` (`"2"`/`"3"`, probed), `window` (`7d`), `algorithm_cpu`/`algorithm_ram` (`max`/`quantile`), `q_cpu`/`q_ram`, `target_cpu_utilization`/`target_ram_utilization` (`0.7`), `filter`, `cluster_id` (required when Kubecost reports several clusters), `abandoned_workloads`/`node_group_sizing` (`true`), `paths` (per-endpoint overrides), `timeout_seconds` (`30`). See [Kubecost](sources/kubecost.md). |

```yaml
sources:
  settings:
    greenkube.carbon_aware: { enabled: false }   # turn one native analyzer off (its active advice goes stale)
    vpa: { enabled: auto }
    kubecost: { url: "http://kubecost-cost-analyzer.kubecost:9090", cluster_id: "prod-eu" }
    push: { max_age_hours: 72 }
```

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

Automation is always on (roadmap F12): discovery runs whenever Argo CD or Flux is present, and pull-request
delivery activates within 60 seconds of creating the credentials Secret (read by name through the Kubernetes
API), without upgrade or restart. The
controller's `automation-readiness` loop infers the provider of each discovered repository, enables it while the
Secret is mounted, expands `@discovered` and reports every capability in `GET /api/v1/setup/status`. The full
workflow, supported source layouts, and security model are documented in [GitOps automation](automation.md). The
most important values are:

| Key | Default | Description |
|-----|---------|-------------|
| `automation.credentialsSecret` | `greenkube-git-credentials` | `GITOPS_DEFAULT_CREDENTIALS_SECRET`. Secret in the release namespace read for inferred providers: key `token`, or keys `appId` + `privateKey` for a [GitHub App](automation.md#github-app) (bound only to a GitHub host), and an optional key `host` (or `apiBaseUrl`) naming the one Git host the credentials are sent to. Mounted as an optional volume into the api, controller and worker. |
| `automation.delivery.mode` | `direct` | `AUTOMATION_DELIVERY_MODE`. Who opens the pull request of an approved plan ([Delivery modes](automation.md#delivery-modes)): `direct` (GreenKube, needs write credentials), `patch` (an operator applies the downloaded `git format-patch`) or `ci` (a CI job polls approved plans and reports the PR back). `patch` and `ci` need read access only. Recorded on each plan when it is approved. |
| `automation.autoApprove.enabled` | `false` | `AUTOMATION_AUTO_APPROVE_ENABLED`. Approve a preview without the extra click when every recommendation meets the limits below ([Auto-approval](automation.md#auto-approval)). The requester still needs the `approve` scope; rollbacks are never auto-approved. |
| `automation.autoApprove.maxRisk` | `low` | `AUTOMATION_AUTO_APPROVE_MAX_RISK`. Highest risk level approved automatically (`low`, `medium`, `high`). |
| `automation.autoApprove.minConfidence` | `0.8` | `AUTOMATION_AUTO_APPROVE_MIN_CONFIDENCE`. Lowest confidence approved automatically (0–1). |
| `automation.autoApprove.increases` | `false` | `AUTOMATION_AUTO_APPROVE_INCREASES`. Also auto-approve request increases (reliability fixes that cost money, roadmap H1). Off: a person approves every increase. |
| `automation.autoApprove.contested` | `false` | Contested consensus is never auto-approved; the schema accepts only `false`. A decrease also needs GreenKube's own usage evidence or at least two independent agreeing sources. |
| `automation.repositoryAllowlist` | `["@discovered"]` | `AUTOMATION_REPOSITORY_ALLOWLIST`. Repositories PRs may target, for inferred providers and providers without their own allowlist. `@discovered` means every repository referenced by a discovered Argo CD/Flux source; an explicit list of `owner/repo` names or segment globs narrows it. Repositories allowed from the setup checklist (`POST /api/v1/automation/repositories/allowlist`) are added. |
| `automation.policy.pauseDelivery` | `false` | `AUTOMATION_PAUSE_DELIVERY`. The only off switch: plan approval returns `delivery_paused` (409) and no PR is opened, while discovery, previews and measurement keep running. |
| `automation.discoveryInterval` | `5m` | Discovery refresh cadence (`1s`–`1h`). |
| `automation.bindingTtl` | `30m` | Binding and manual-resolution freshness (`1s`–`24h`). |
| `automation.localClusterAliases` | `[]` | `GITOPS_LOCAL_CLUSTER_ALIASES` (JSON list). Extra Argo CD destination server URLs or cluster names under which this cluster is registered, besides `https://kubernetes.default.svc` and `in-cluster`. An Application deploying to any other cluster, and a Flux Kustomization or HelmRelease with `spec.kubeConfig`, never binds a workload or resource of this cluster (`destination_remote`). |
| `automation.prPollIntervalSeconds` | `120` | PR polling fallback interval (10–86400). |
| `automation.syncPollIntervalSeconds` | `30` | Controller sync observation interval (10–86400). |
| `automation.syncTimeoutSeconds` | `1800` | Time before a merged but unsynced plan becomes `sync_failed` (60–86400). |
| `automation.providers` | `[]` | Explicit Git providers; they win over inference for their host. See below. |
| `automation.webhooks.enabled` | `false` | Enable signed provider webhook ingestion. |
| `automation.policy` | See below | Risk, confidence, sample, coverage, PR-budget, cooldown, and freeze gates. |
| `automation.pullRequests` | See below | Draft, label, reviewer, and team-reviewer options. |
| `automation.helm.remoteCharts.enabled` | `true` | `GITOPS_HELM_REMOTE_CHARTS`. Pre-fetch, digest-verify and render the remote chart (HTTP repository or OCI registry) a discovered Argo CD/Flux source deployed ([Helm support](automation.md#helm-support)). `false` makes remote charts fail closed (`remote-charts-disabled`). |
| `automation.helm.remoteCharts.allowedHosts` | `[]` | `GITOPS_HELM_CHART_HOSTS` (JSON list). Hosts the controller may fetch charts from: `host`, `host:port` or `*.domain`. Empty: any public host named by a discovered chart source; loopback, link-local and metadata addresses are always refused and plain-HTTP repositories must be listed. Docker Hub charts also need `auth.docker.io` and `*.docker.com` when set. |
| `automation.helm.chartMappings` | `[]` | `GITOPS_HELM_CHART_MAPPINGS` (JSON list). Values paths per chart and version (`chart`, `version`, `container`, `path` or `paths`; globs allowed, the path ends with `resources`), tried before the built-in registry and render-validated like any mapping. |
| `automation.pr.titlePrefix` | `greenkube` | `GITOPS_PR_TITLE_PREFIX`: prefix of PR titles (at most 32 characters); empty drops it. |
| `automation.pr.template` | `''` | `GITOPS_PR_TEMPLATE`: PR body template with `{{ section }}` placeholders; empty uses the built-in template. See [Pull-request content](automation.md#pull-request-content). |
| `automation.pr.defaultLabels` | `true` | `GITOPS_PR_DEFAULT_LABELS`: add `greenkube`, `optimization/<capability>`, `risk/<level>` and `reliability` labels. |
| `automation.pr.draftHighRisk` | `true` | `GITOPS_PR_DRAFT_HIGH_RISK`: open high-risk plans as draft PRs. |
| `automation.pr.codeowners` | `true` | `GITOPS_PR_CODEOWNERS`: request reviews from the CODEOWNERS of the touched paths. |
| `automation.pr.commitStatus` | `true` | `GITOPS_PR_COMMIT_STATUS`: publish the `greenkube/validation` commit status on the PR head. |
| `automation.pr.lifecycleComments` | `true` | `GITOPS_PR_LIFECYCLE_COMMENTS`: keep one GreenKube status comment up to date on merged PRs, and on open PRs whose plan has newer advice queued (E7). |
| `automation.pr.lifecycleLookbackDays` | `30` | `GITOPS_PR_LIFECYCLE_LOOKBACK_DAYS`: how long after the merge the status comment is refreshed (1–365). |

Configure providers through Secret references:

```yaml
automation:
  providers:
    - id: github-main
      type: github
      apiBaseUrl: https://api.github.com
      secretName: greenkube-git
      secretKey: token
      repositoryAllowlist:
        - "acme/gitops"
```

A provider without `repositoryAllowlist` uses `automation.repositoryAllowlist`; `@discovered` in a provider allowlist adds the discovered repositories of its host. Glob matching is segment-aware: `acme/*` matches `acme/repo`, not `acme/team/repo`. Only the provider ID, API/web base URLs, Secret reference, and allowlist are rendered into the ConfigMap. The token stays in the referenced Kubernetes Secret and is mounted read-only at `/etc/greenkube/git-providers/<secret-name>/<secret-key>`; when `secretNamespace` is set, the mount path includes that namespace before the Secret name. The Secret must be in the Helm release namespace. Argo CD and Flux discovery select a configured provider only when its allowlist matches the source repository. Preview and approval remain unavailable for missing credentials, unsupported renderers, or ambiguous sources.

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

#### Isolated renderer (`renderer`)

The chart always runs Helm/Kustomize rendering in a separate `<release>-renderer` Pod (raw-manifest repositories never call it). It receives only a bounded repository snapshot; it does not mount provider or database Secrets and does not receive a Kubernetes service-account token. Its root filesystem is read-only and its process/resource/output limits are bounded. The renderer NetworkPolicy denies egress and allows requests only from GreenKube Pods; at startup and then daily, the renderer checks that the policy is enforced with a canary TCP connection to the cluster DNS Service (`GET /status` of the renderer); when it is not, Kustomize and Helm previews still work, but every Kustomize and Helm plan carries the warning `renderer_egress_unenforced` in `file_plan.warnings` (bound by the plan digest the approver echoes back) and the setup checklist reports it. The plan service reads the state the controller's readiness reconciler last stored, never the renderer itself. The renderer holds no Secrets and no token, Helm runs in template mode without `lookup`, and remote Kustomize bases fail closed. Remote Helm charts never make the renderer reach the network: the controller fetches them (with the same egress it uses for Git providers; add the chart registries to `monitoring.networkPolicy.additionalEgress` when that policy is enabled), verifies their digest and stages the unpacked chart into the snapshot it sends to the renderer. For clusters that permit `NET_ADMIN` but do not enforce NetworkPolicy, `renderer.egressFirewall.enabled` can install IPv4/IPv6 output-deny rules with a one-shot init container; the renderer process itself remains non-root with all capabilities dropped. `renderer.egressFirewall.privilegedInitContainer` is an opt-in local-cluster workaround for runtimes that deny `NET_ADMIN` unless the init container is privileged. Do not enable it in production; use a NetworkPolicy-enforcing CNI instead. The API/controller fails closed when no isolated renderer answers.

The default snapshot limits are 100 MiB compressed, 500 MiB extracted, 20,000 files, and 2 MiB per file. `renderer.tmpSizeLimit` bounds the renderer's temporary workspace. The service accepts only the pinned `helm` and `kustomize` commands. Kustomize `helmCharts` remain unsupported because their embedded Helm execution does not yet apply the direct Helm renderer's full safety checks.

Provider snapshots use the GitHub, GitLab, or Gitea archive endpoint pinned to the full commit SHA resolved from the configured ref. The archive body is streamed with the compressed-byte limit before extraction; the extractor normalizes the provider's single repository-root directory and enforces the extracted-byte, file-count, and per-file limits while rejecting unsafe paths and filesystem entries. Archive requests run through the authorized repository-scoped provider, and cross-origin downloads never receive provider authorization headers. Providers that do not support bounded immutable-commit archives fail closed; GreenKube does not retry at the mutable branch name or fall back to per-file reads.

The Helm renderer receives the Kubernetes version and API-version set discovered by the Helm chart at install/upgrade time, so capability-gated templates are rendered against the target cluster. Non-Helm deployments can set `GITOPS_HELM_KUBE_VERSION` and `GITOPS_HELM_API_VERSIONS_JSON` (a JSON array) explicitly.

```yaml
renderer:
  replicas: 1
  servicePort: 8090
  tmpSizeLimit: 768Mi
  egressFirewall:
    enabled: false
    privilegedInitContainer: false
  resources:
    requests:
      cpu: 25m
      memory: 96Mi
    limits:
      cpu: 500m
      memory: 1Gi
```

The local Bubblewrap backend is used only when no remote renderer URL is configured and the runtime kernel permits the required unprivileged namespaces. It fails closed rather than falling back to an unrestricted subprocess.

#### Automation policy (`automation.policy`)

Policy is evaluated on preview/plan creation and revalidated on approval. Risk, confidence, sample/coverage, open-PR budget, cooldown, change-freeze labels, and absolute freeze windows are configurable. Discovery also supplies live workload labels, rollout-in-progress state, controller health/suspend state, and HPA, VPA and PDB evidence. A configured freeze label blocks only matching workloads. Rollout, controller-health, HPA, VPA and PDB gates reject a plan when their evidence reports a conflict. An HPA blocks only the resource it scales on with a `Utilization` target (a share of the request; `AverageValue` and `Value` targets compare raw usage, so they only warn), every HPA that targets the workload is checked, and a VPA blocks only in an active update mode (`Auto`, `Recreate`, `InPlaceOrRecreate`, `InPlace`). See [GitOps automation](automation.md#approval-and-policy). The defaults require medium-or-lower risk, at least 0.6 confidence, `config.recommendations.minSamples` samples (36), and 50% coverage.

`minCoverage` applies to the evidence attached to each recommendation: the observed CPU or memory samples divided by the expected observation points in that recommendation's window. `minSamples` is the minimum sample count for the recommendation. This is separate from GitOps discovery coverage (`GET /api/v1/gitops/status`), which measures resolved workload bindings. Lowering coverage or sample thresholds can make more PRs eligible but reduces the amount of observability backing each change.

```yaml
automation:
  policy:
    maxRiskLevel: medium
    pauseDelivery: false
    minConfidence: 0.6
    minCoverage: 0.5
    maxOpenPullRequestsPerWorkload: 1
    cooldownMinutes: 60
    changeFreezeLabels: []
    changeFreezeWindows: []
    missingEvidence: fail
```

`missingEvidence` sets what the live gates (rollout, controller health, HPA, VPA, PDB) do when discovery could not collect their evidence:

| Value | Behavior |
|---|---|
| `fail` | Reject the plan with `policy_evidence_missing`. |
| `warn` | Allow the plan, mark the gate `not_available`, and add a warning to `policy_result.warnings`. |

The default is `fail`, with or without Helm: an unset value in `GITOPS_POLICY_JSON` also means `fail` (principle 6, fail closed). Any other value is rejected by the values schema. Set `warn` only where discovery cannot read rollouts, HPAs, VPAs or PDBs, such as a sandbox without the discovery RBAC.

#### Pull request options (`automation.pullRequests`)

```yaml
automation:
  pullRequests:
    draft: false
    labels: []
    reviewers: []
    teamReviewers: []
```

Configured labels are added on top of the derived `greenkube`, `optimization/<capability>`, `risk/<level>` and `reliability` labels (`automation.pr.defaultLabels`). They no longer have to exist beforehand: GitHub and GitLab create missing labels, and GreenKube creates them through the Gitea label API. Configured reviewers are requested in addition to the CODEOWNERS of the touched paths and are the fallback when there are none.

GitHub supports draft PRs, labels, reviewers, and team reviewers. GitLab supports draft MRs, labels, and numeric reviewer IDs; team reviewers are unsupported. Gitea supports labels and reviewers; draft PRs and team reviewers are unsupported. Unsupported combinations fail before the provider creates a branch or commit.

### Secrets (`secrets`)

| Key | Description |
|-----|-------------|
| `secrets.apiKey` | Deprecated bootstrap API key `GREENKUBE_API_KEY` (what it may do: `auth.legacyApiKey`), rendered into the Secret `<release>-values`. When omitted, the `<release>-credentials-bootstrap` hook generates one once into the Secret `<release>` **only with `config.apiAuthMode: api_key`**, where it is the only administrator (an existing generated key is kept on upgrade); upgrades and GitOps syncs never rotate it. In `api_key` mode an existing Secret must contain `GREENKUBE_API_KEY`; without it, production requests fail closed with `503`. |
| `secrets.metricsToken` | Prometheus scrape token `GREENKUBE_METRICS_TOKEN` (`metrics:read` only), sent by the ServiceMonitors to the API, controller and worker metrics endpoints. When omitted, the credentials bootstrap hook generates one once into the Secret `<release>`. |
| `secrets.existingSecretHasMetricsToken` | `false`. With `secrets.existingSecret`: `true` when that Secret holds `GREENKUBE_METRICS_TOKEN`; the ServiceMonitors then scrape with it and the components check it. Otherwise they keep scraping with its `GREENKUBE_API_KEY`. |
| `secrets.electricityMapsToken` | Electricity Maps API token when `config.electricityProvider` is `electricity_maps`. Without it, GreenKube uses bundled zone-specific fallback data when available, then the configured global intensity. Get a token at [electricitymaps.com](https://www.electricitymaps.com/). |
| `secrets.wattnetEmail` | Wattnet account email (only when `config.electricityProvider` is `wattnet`). Register for free at [api.wattnet.eu/token-request/register](https://api.wattnet.eu/token-request/register) |
| `secrets.wattnetPassword` | Wattnet account password (only when `config.electricityProvider` is `wattnet`) |
| `secrets.boaviztaToken` | Optional Boavizta API token. |
| `secrets.existingSecret` | Name of an existing Kubernetes Secret to use instead of chart-rendered and generated credentials; the GitOps path. The chart renders no Secret and runs no credentials bootstrap hook, so include every credential the installation needs (for the bundled database: `POSTGRES_PASSWORD` and `DB_CONNECTION_STRING`). |
| `secrets.dbConnectionString` | PostgreSQL connection string when using an external PostgreSQL instance (`postgres.enabled: false`) with the chart-managed Secret. When `secrets.existingSecret` is set, put `DB_CONNECTION_STRING` in that Secret instead. |
| `secrets.prometheus.username`, `secrets.prometheus.password`, `secrets.prometheus.bearerToken` | Optional Prometheus authentication credentials. |

Rendering never uses `lookup` or random functions, so `helm template` output is stable under Argo CD and Flux. Values set above go to the Secret `<release>-values`; generated credentials (the scrape token `GREENKUBE_METRICS_TOKEN`, `GREENKUBE_API_KEY` in `api_key` mode, and `POSTGRES_PASSWORD` with `DB_CONNECTION_STRING` for the bundled database) are created once by the credentials bootstrap hook in the Secret `<release>`, which is not part of the release and survives `helm uninstall`. See [Credentials and GitOps](installation.md#credentials-and-gitops-argo-cd-flux).

### PostgreSQL network policy (`postgres.networkPolicy`)

| Key | Default | Description |
|-----|---------|-------------|
| `postgres.networkPolicy.enabled` | `true` | Ingress NetworkPolicy of the bundled PostgreSQL: only the release's `api`, `controller` and `worker` Pods (and backup or copy Jobs labelled `greenkube.cloud/postgres-client: "true"`) reach port 5432. Needs a CNI that enforces NetworkPolicy. |
| `postgres.networkPolicy.additionalIngress` | `[]` | Extra NetworkPolicy peers (`podSelector`, `namespaceSelector`, `ipBlock`) allowed to connect. |

### Controller metrics endpoint (`controller.metricsPort`)

| Key | Default | Description |
|-----|---------|-------------|
| `controller.metricsPort` | `9102` | `CONTROLLER_METRICS_PORT`. Port of the controller's Prometheus endpoint at `/prometheus/metrics` (also served at `/metrics`) (collection source requests/errors, collection run duration, provenance mix of written rows, recommendation scans and optimization runs, verification outcomes, GitOps sync telemetry). Served through the `<release>-controller-metrics` ClusterIP Service and scraped by a dedicated ServiceMonitor when `monitoring.serviceMonitor.enabled` `0` disables the listener, the Service and the ServiceMonitor; the controller probes then fall back to a process check. Replaces `collector.metricsPort` (mapped for one minor release). |

Recommendation scans and verification run in the controller, so their metrics (`greenkube_optimization_runs_total`, `greenkube_last_successful_optimization_run_timestamp_seconds`, `greenkube_verification_outcomes_total`, ...) are served by this endpoint, not by the API. Every controller replica serves its registry, leader or standby.

The chart keeps the API key out of the controller (least privilege), so this endpoint is reachable on the pod network only and serves bounded, non-sensitive counters; enable `monitoring.networkPolicy` to restrict it to Prometheus. When an API key is present in the controller's environment, scrapes must send it as a bearer token.

### Monitoring (`monitoring`)

```yaml
monitoring:
  prometheusRule:
    enabled: false       # Optional automation backlog/stuck-state alerts; requires the PrometheusRule CRD
    controllerAlerts: true # Controller loop alerts (rendered only with the controller ServiceMonitor)
    thresholds:          # Loop health alerts (group greenkube.loop), rendered with the controller alerts
      collectionLagSeconds: 1800
      recommendationScanStaleSeconds: 10800
      measurementStaleSeconds: 1800
    operations:          # Operations alerts (group greenkube.operations)
      enabled: true
      kubeStateMetrics: true    # false when kube-state-metrics is not scraped
      volumeFreeRatio: 0.15
      volumeFullWithinHours: 96
      backupMaxAgeHours: 36
    namespace: monitoring
    additionalLabels: {}
  serviceMonitor:
    enabled: false       # Set to true if using the Prometheus Operator (kube-prometheus-stack)
    namespace: ""        # Empty: the release namespace, next to the scrape token Secret
    interval: 30s
    prometheusServiceAccount: prometheus-k8s
    prometheusNamespace: ""  # Namespace of that ServiceAccount; empty: networkPolicy.prometheusNamespace
  networkPolicy:
    enabled: false       # Restrict selected components to explicit ingress and egress rules
    prometheusNamespace: monitoring
    additionalIngress: []
    additionalEgress: [] # Add narrow rules for external DB, Git provider, or Kubernetes API endpoints.
```

**ServiceMonitor namespace and scrape token.** Prometheus Operator reads a ServiceMonitor's bearer token Secret in the ServiceMonitor's own namespace and rejects the ServiceMonitor otherwise (`secret not found`, no target). The ServiceMonitors therefore default to the release namespace, where the scrape token Secret `<release>` lives; Prometheus must select that namespace (`serviceMonitorNamespaceSelector`, which kube-prometheus-stack leaves open to every namespace by default; its `serviceMonitorSelector` still needs your `additionalLabels`, e.g. `release: <stack release>`). With `serviceMonitor.namespace` set to another namespace, the chart keeps a copy of the scrape token there, Secret `<release>-scrape-token` with key `token`, and every ServiceMonitor uses it:

| Scrape token source | How the copy is made |
| --- | --- |
| Generated (default) | The credentials bootstrap hook copies it on every install and upgrade, with a Role and RoleBinding scoped to that Secret in the ServiceMonitor namespace (hook resources, deleted after the run). The copy changes only when the source does, and stays after `helm uninstall` like the Secret `<release>`. |
| `secrets.metricsToken` | Rendered by the chart in the ServiceMonitor namespace (a release resource). |
| `secrets.existingSecret` with `existingSecretHasMetricsToken: true` | Copied by the hook, which may then read that Secret (needs `rbac.create`). |
| None (`existingSecret` without a scrape token, or `rbac.create: false` with an existing Secret) | Nothing is copied, and never the deprecated admin key: create the referenced Secret in the ServiceMonitor namespace yourself, set `bearerTokenSecret`, or leave `namespace` empty. The install notes print a warning. |

`serviceMonitor.prometheusServiceAccount` and `serviceMonitor.prometheusNamespace` name the Prometheus ServiceAccount bound to the `prometheus-k8s` Role (Service, Pod and EndpointSlice discovery in the release namespace). Its namespace is independent of the ServiceMonitor namespace: `prometheusNamespace`, else `networkPolicy.prometheusNamespace`, else `monitoring`. Upgrading from a chart that put the ServiceMonitors in `monitoring` by default: see [Upgrading](upgrading.md#servicemonitors-and-the-scrape-token).

The controller's own metrics endpoint is configured with `controller.metricsPort` (see [Controller metrics endpoint](#controller-metrics-endpoint-controllermetricsport)); `serviceMonitor.enabled` also adds its ServiceMonitor.

`prometheusRule.enabled` installs alerts for stuck plan operations, plans awaiting controller sync, failed syncs, non-terminal webhook deliveries, and stale worker heartbeats. With `prometheusRule.controllerAlerts` (default `true`) it also installs the controller loop alerts `GreenKubeControllerLoopFailing` (a loop failed 3 ticks in a row for 10 minutes), `GreenKubeAutomationLoopStale` (the automation controller loop has not succeeded for 10 minutes) and `GreenKubeNoActiveController` (no controller holds the Lease, or none is scraped, for 5 minutes); they read the controller metrics endpoint, so they render only together with the controller ServiceMonitor (`serviceMonitor.enabled` and `collector.metricsPort` > 0). Together with the controller loop alerts it installs the loop health alerts (group `greenkube.loop`, roadmap N7): `GreenKubeCollectionLagging` (the leader's `greenkube_collection_watermark_lag_seconds{source}` above `thresholds.collectionLagSeconds` for 15 minutes), `GreenKubeRecommendationScanFailing` (every scan of the last hour failed), `GreenKubeRecommendationSourceFailing` (a source failed during scans for over an hour), `GreenKubeRecommendationScanStale` (no successful scan for `thresholds.recommendationScanStaleSeconds`), `GreenKubeMeasurementStalled` (the `recommendation-lifecycle` loop, which runs apply detection, verification and change measurement, has not succeeded for `thresholds.measurementStaleSeconds`), `GreenKubeGitProviderRateLimited` (a Git provider kept rate-limiting GreenKube for 30 minutes) and, whenever a GreenKube process is scraped, the ledger alerts (group `greenkube.ledger`) `GreenKubeLedgerChainVerificationFailed` (critical: `greenkube_ledger_chain_ok{cluster_uid}` is 0) and `GreenKubeLedgerProjectionBacklog` (v1 accrual rows still missing from the ledger v2 after 3 hours). `prometheusRule.operations` (default on) adds the group `greenkube.operations`: `GreenKubeAPIDown` (no API target up, with the ServiceMonitor), `GreenKubeDatabaseUnavailable` (the controller heartbeat cannot be written, with the controller ServiceMonitor), `GreenKubeAPIUnavailable` and `GreenKubePostgresDown` (kube-state-metrics; `operations.kubeStateMetrics: false` leaves them out), `GreenKubeBackupMissing` (with `postgres.backup.enabled`), and `GreenKubeDataVolumeLow` / `GreenKubeDataVolumeFillingUp` (kubelet volume stats of the bundled PostgreSQL and backup claims); details in [Operations alerts](observability-slos.md#operations-alerts). See [Observability and SLOs](observability-slos.md#chart-alerts). The corresponding `greenkube_automation_backlog{queue,state}` gauges and heartbeat age are exposed on `/prometheus/metrics`; enable `serviceMonitor` separately to scrape them.

When enabled, the chart applies policies to the API, controller and (optional) worker pods. It allows DNS and the bundled PostgreSQL connection, plus Prometheus and the ready-check ingress paths; other external egress requires an explicit `additionalEgress` rule. No unrestricted internet egress is added.

### Exposure (`ingress`, `httpRoute`)

Optional, off by default (roadmap N6). Both route their hosts to the `<release>-api` Service, which serves the UI and the API, under `config.rootPath` (or `/`).

| Key | Default | Description |
| --- | --- | --- |
| `ingress.enabled` | `false` | Render a `networking.k8s.io/v1` Ingress. |
| `ingress.className` | `""` | IngressClass; empty uses the cluster default. |
| `ingress.hosts` | `[]` | Host names (required when enabled). The first one sets `GREENKUBE_EXTERNAL_URL` unless `config.externalUrl` is set. |
| `ingress.tls` | `[]` | Standard Ingress TLS entries (`secretName`, `hosts`). The external URL uses `https` when an entry lists the first host or lists no hosts. |
| `ingress.annotations`, `ingress.labels` | `{}` | Added to the Ingress (e.g. `cert-manager.io/cluster-issuer`). |
| `httpRoute.enabled` | `false` | Render a Gateway API `gateway.networking.k8s.io/v1` HTTPRoute (needs the Gateway API CRDs). |
| `httpRoute.parentRefs` | `[]` | Gateways to attach to (required when enabled), e.g. `[{name: shared-gateway, namespace: gateway-system, sectionName: https}]`. |
| `httpRoute.hostnames` | `[]` | Host names; the first one sets `GREENKUBE_EXTERNAL_URL` when no Ingress host or `config.externalUrl` does. |
| `httpRoute.scheme` | `https` | Scheme of the URL built from the first hostname (`http` when the listener does not terminate TLS). |
| `httpRoute.annotations`, `httpRoute.labels` | `{}` | Added to the HTTPRoute. |

`GREENKUBE_EXTERNAL_URL` (pull-request deep links) is, in order: `config.externalUrl`; the first Ingress host; the first HTTPRoute hostname; empty. A wildcard first host (`*.example.com`) builds no URL. With `monitoring.networkPolicy.enabled`, allow the ingress controller or gateway pods in `monitoring.networkPolicy.additionalIngress`.

### PostgreSQL StatefulSet (`postgres`)

GreenKube ships with an optional bundled PostgreSQL StatefulSet. Adjust storage and credentials as needed:

```yaml
postgres:
  enabled: true
  image:
    repository: postgres
    tag: 18-alpine@sha256:1018733457011bf88d81db98cf43aac6d5150afc18787dbe204771fedf80ff95
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

## Production settings

There is no production profile: the default topology is production-grade. For production:

- set `api.replicas: 2` and `controller.replicas: 2` (only the Lease holder runs controller jobs, including
  recommendation scans, GitOps discovery and sync; a standby takes over within `leaseDurationSeconds`);
- create the database credentials in a Kubernetes Secret and set `secrets.existingSecret` (with the bundled
  PostgreSQL it must contain `POSTGRES_PASSWORD` and `DB_CONNECTION_STRING`; with an external one,
  `DB_CONNECTION_STRING`), and `auth.admin.existingSecret` for a fully declarative install;
- serve the UI over HTTPS (see [Ingress and TLS](installation.md#optional-ingress-and-tls));
- turn on `postgres.backup.enabled` and the alerts, and size the data volume: the
  [production checklist](installation.md#production-checklist) lists every item.

See the [Installation guide](installation.md#credentials-and-gitops-argo-cd-flux).

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
| `auth.session.secureCookie` | `auto` | `SESSION_COOKIE_SECURE` | `auto` (HTTPS directly or via `config.trustedProxyIps`), `always`, `never`. `true`/`false` are accepted as `always`/`never`. With an HTTPS exposure (`ingress.enabled` with `ingress.tls`, or `httpRoute.enabled` with `httpRoute.scheme: https`) the chart renders `auto` as `always`. |
| `auth.session.hsts.maxAge` | `31536000` | `HSTS_MAX_AGE` | `max-age` of `Strict-Transport-Security`, sent on HTTPS responses and, with `SESSION_COOKIE_SECURE=always`, on every response. `0` disables the header. |
| `auth.session.hsts.includeSubdomains` | `false` | `HSTS_INCLUDE_SUBDOMAINS` | Add `includeSubDomains` to the header. |
| `auth.session.idleTimeout` | `12h` | `AUTH_SESSION_IDLE_TIMEOUT` | Sliding inactivity timeout (`<n>s\|m\|h\|d`, `m` = minutes). |
| `auth.session.absoluteLifetime` | `7d` | `AUTH_SESSION_ABSOLUTE_LIFETIME` | Maximum session lifetime. |
| `auth.session.rememberLifetime` | `30d` | `AUTH_SESSION_REMEMBER_LIFETIME` | Lifetime with **Keep me signed in**. |
| `auth.login.rateLimit` | `5/minute` | `AUTH_LOGIN_RATE_LIMIT` | Failed sign-ins per username and client IP before an exponential lockout of that pair; each client IP also gets four times as many across usernames (a plain window). |
| `auth.legacyApiKey` | `admin` | `AUTH_LEGACY_API_KEY` | What the deprecated bootstrap key `GREENKUBE_API_KEY` may do: `admin` (full admin, as before; required with `apiAuthMode: api_key`), `metrics` (scrape only) or `disabled` (rejected). See [The bootstrap API key](authentication.md#the-bootstrap-api-key). |
| `auth.localLogin` | `true` | `AUTH_LOCAL_LOGIN` | In `proxy` and `oidc` modes, local accounts (the break-glass admin) still sign in with a password. `false`: SSO only (`POST /auth/login` answers `409`). |
| `auth.roles.groups.{admin,operator,viewer}` | `[]` | `AUTH_ROLE_GROUPS` | IdP groups granting each role (rendered as a JSON object role to groups). An SSO user gets the highest role any of its groups grants, recomputed at every sign-in. |
| `auth.roles.defaultRole` | `viewer` | `AUTH_DEFAULT_ROLE` | Role of an SSO user in no listed group: `viewer`, `operator`, `admin`, or `none` (access refused). |
| `auth.proxy.userHeaders` | `X-Forwarded-User` | `AUTH_PROXY_USER_HEADERS` | Identity header the authentication proxy sets and overwrites (`apiAuthMode: proxy`). Several comma-separated headers form separate families; a request carrying more than one family is refused. |
| `auth.proxy.groupsHeaders` | `X-Forwarded-Groups` | `AUTH_PROXY_GROUPS_HEADERS` | Groups header paired with each user header (same position); never more entries than `userHeaders`. |
| `auth.oidc.issuer` | `""` | `OIDC_ISSUER` | OpenID Connect issuer (`apiAuthMode: oidc`, required, `https`). Discovery reads `<issuer>/.well-known/openid-configuration`. |
| `auth.oidc.clientId` | `""` | `OIDC_AUDIENCE` | OAuth client id registered at the provider (required with `oidc`). |
| `auth.oidc.existingSecret` | `""` | `OIDC_CLIENT_SECRET` | Secret holding the client secret (key `auth.oidc.existingSecretKey`, default `clientSecret`), mounted into the API. Empty: public client protected by PKCE. |
| `auth.oidc.scopes` | `openid profile email` | `OIDC_SCOPES` | Requested scopes (`openid` is always added). Add your provider's groups scope when groups are not in the ID token by default. |
| `auth.oidc.usernameClaim` | `preferred_username` | `OIDC_USERNAME_CLAIM` | Claim used as the username (then `preferred_username`, `email`, `sub`). The account is keyed by the issuer's `sub`. |
| `auth.oidc.groupsClaim` | `groups` | `OIDC_GROUPS_CLAIM` | Claim holding the groups (a list, or a comma-separated string). |
| `auth.oidc.redirectUrl` | `""` | `OIDC_REDIRECT_URL` | Redirect URI registered at the provider. Empty: `<config.externalUrl>/api/v1/auth/oidc/callback`, or the request's origin when `externalUrl` is empty. |
| `auth.oidc.providerName` | `SSO` | `OIDC_PROVIDER_NAME` | Label of the **Sign in with ...** button. |
| `monitoring.serviceMonitor.bearerTokenSecret` | `{name: "", key: token}` | | Secret with a `metrics:read` API key for the API scrape, in the ServiceMonitor namespace. Empty uses the scrape token `GREENKUBE_METRICS_TOKEN` (its copy `<release>-scrape-token` outside the release namespace; without a scrape token, the deprecated bootstrap key). The controller and worker ServiceMonitors always use the scrape token. See [Monitoring](#monitoring-monitoring). |
| `secrets.metricsToken` | `""` | `GREENKUBE_METRICS_TOKEN` | See [Secrets](#secrets-secrets). |

`SESSION_COOKIE_NAME` (default `greenkube_session`) is not exposed in values.

The chart defaults to `environment: production`, disables GitOps automation until explicitly enabled, and generates a random Prometheus scrape token once (credentials bootstrap hook, Secret `<release>`); only `apiAuthMode: api_key` also generates the deprecated bootstrap API key. People sign in with accounts and machines use scoped API keys. For local development, `config.environment: development` leaves an instance without any account and without a bootstrap key open; do not expose it publicly. See [Authentication](authentication.md).

### Data sources are configuration as code

Data-source endpoints and tokens (Prometheus, OpenCost, Boavizta, Electricity Maps, Wattnet) are set in Helm values or Secrets only (roadmap A13). The Settings page and the connectivity popup show them read-only with copy-paste Helm snippets; `POST /api/v1/config/services` answers `410 Gone`. Runtime edits reached only the API process while the controller kept its collectors (BUG-11), and writing them into the release Secret drifted from Git (BUG-60); the API no longer has any RBAC to write that Secret.

With the chart, Pods read each service URL and token from the first of these that holds it:

1. The Secret `<release>-values`, when you set the key in values (`config.prometheus.url`, `config.opencost.url`, `config.boavizta.url`, `secrets.*`).
2. The Secret `<release>`, which may still hold overrides saved by the Settings page of an earlier release.
3. The built-in default or in-cluster discovery.

To change a data source, set it in values (or in your `secrets.existingSecret`) and upgrade. To drop an old Settings-page override from the Secret `<release>`, remove its key, for example `kubectl -n greenkube patch secret greenkube --type=json -p='[{"op":"remove","path":"/data/PROMETHEUS_URL"}]'`.

The API expires unapproved GitOps plans once per minute. A daily retention sweep removes rendered file contents and terminal operation/webhook rows older than 90 days while retaining plan metadata, pull-request records, and recommendation audit events. Worker heartbeat rows older than seven days are pruned when the worker starts.

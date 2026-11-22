# Upgrading

## Before every upgrade: back up the database

**This step is mandatory.** The controller migrates the database schema at startup, migrations only move
forward, and `helm rollback` does not undo them: the only way back from a failed upgrade is to restore a backup
taken before it ([Backup and restore](operations/backup-restore.md#roll-back-an-upgrade)).

```bash
# With scheduled backups (postgres.backup.enabled=true): run one now and wait for it.
kubectl -n greenkube create job --from=cronjob/greenkube-postgres-backup backup-before-upgrade
kubectl -n greenkube wait --for=condition=complete job/backup-before-upgrade --timeout=1h

# Without them (0.3.x has none): dump the GreenKube schema (config.db.schema, "public"
# by default) of the bundled PostgreSQL to your workstation.
kubectl -n greenkube exec greenkube-postgres-0 -c postgres -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema=public --format=custom --no-owner' \
  > greenkube-before-upgrade.dump
pg_restore --list greenkube-before-upgrade.dump > /dev/null && echo "backup readable"
kubectl -n greenkube get secret greenkube -o yaml > greenkube-secret.yaml   # store encrypted

helm -n greenkube history greenkube   # note the revision to roll back to
```

With an external PostgreSQL, use your provider's snapshot or `pg_dump --schema=<config.db.schema>` with the
same connection string. A rollback restores this dump into an emptied schema (`DROP SCHEMA ... CASCADE` and the
dump in one transaction, see [Restore](operations/backup-restore.md#restore)): `pg_restore --clean` over the
upgraded schema fails, because the objects that newer migrations created are not in the dump. Then:

- Check the free space of the data volume: a migration that backfills a large table temporarily needs about
  as much free space as that table ([Sizing](installation.md#sizing)).
- Upgrade with `--wait` and a generous `--timeout` (migrations of a large database can take minutes; the
  controller has no statement timeout while it migrates and answers its probes meanwhile).
- Upgrading the bundled PostgreSQL major version (17 to 18) is a separate, explicit procedure:
  [PostgreSQL 17 to 18 upgrade](database-upgrade.md).

## From 0.3.x to 0.4

GreenKube 0.4 replaces the two install profiles with one topology (api, controller, renderer, PostgreSQL) and
turns automation on for everyone: there is no feature flag left, only `automation.policy.pauseDelivery`. Your
data is preserved: the bundled PostgreSQL StatefulSet, its volume and its Secret keep their names.

### What changes

| 0.3.x | 0.4 |
|---|---|
| `profile: standalone` — collector and API in one pod (`<release>`) | `<release>-api` and `<release>-controller` Deployments |
| `profile: production` — api, controller, automation worker, migration Job | the same api and controller Deployments; the worker is optional (`worker.replicas`); no migration Job |
| Migrations in a Helm hook Job (production) or at every pod start | The controller migrates at startup under an advisory lock; API replicas report not-ready (`/api/v1/health` → `migrating`) until the schema is current |
| `automation.enabled`, `automation.mode` | Always on; delivery activates when Git credentials exist; `automation.policy.pauseDelivery` pauses it |
| Mandatory `automation.providers[]` with a non-empty allowlist | Providers inferred from discovered repositories; one Secret `greenkube-git-credentials`; allowlist `["@discovered"]` |
| Renderer only with automation | Renderer always deployed (25m / 96Mi) |
| SQLite in Helm (`config.db.type`, `config.db.path`, `config.persistence`) | PostgreSQL only; SQLite stays for `greenkube demo` and local development |
| Three `minSamples` knobs | One: `config.recommendations.minSamples` |
| `config.recommendations.sourcePriority`, `vpaEnabled`, `karpenterEnabled` | `sources.settings.<id>.enabled`; the source priority is removed (see [Removed connectors](#removed-connectors-and-consensus-policies)) |
| ConfigMap `<release>` (a Helm hook) | ConfigMap `<release>-config` (a regular release resource) |

### Metric measures in double precision (PostgreSQL)

Migration 0145 changes `joules`, `co2e_grams`, `embodied_co2e_grams`, `total_cost`, `pue` and
`grid_intensity` of `combined_metrics`, `combined_metrics_hourly`, `container_metrics` and
`container_metrics_hourly` from `real` (float4) to `double precision`, so sums over millions of rows no
longer drift. Changing the type rewrites the table under an exclusive lock:

- A table of at most 256 MB (indexes included) is converted inside the migration batch, in seconds.
- A larger table is converted online, without a long lock: the migration only adds a `double precision`
  shadow column per measure (`<column>__f8`), kept equal by a trigger, and records the work in
  `online_column_upgrades`. The controller then backfills the shadow columns in the background, 50,000 ids
  per short transaction (progress is recorded, a restart resumes), and swaps them in with one short
  transaction that waits at most 5 s for its lock (otherwise it retries at the next controller start).
  Collection and the API keep working during the backfill; sums keep the float4 drift until the swap.
  `SELECT * FROM online_column_upgrades` is empty once every table is converted.
  The backfill rewrites every row once: table and index size roughly double until autovacuum reclaims the
  old row versions, so keep at least the size of the table free on the database volume. The upgrade lifts the
  session `statement_timeout` (the concurrent index build and constraint validation of a large table take longer
  than `DB_STATEMENT_TIMEOUT_MS`), and a swap that missed its lock is retried every 10 minutes until it succeeds.

Stored values keep their 7 significant digits. SQLite is not affected (its `REAL` is already 8 bytes).

### 64-bit ids (PostgreSQL)

Migration 0147 turns every `integer` id column and its sequence into `bigint`. The sequences change at once.
Tables of at most 256 MB are rewritten inside the migration batch (seconds). `combined_metrics` and
`combined_metrics_hourly` above 256 MB are converted online like the measures above: a `bigint` shadow column
`id__i8` kept equal by a trigger, a batched backfill, a unique index built with `CREATE INDEX CONCURRENTLY`, then a
short swap that re-creates the primary key on that index. Until the swap, these two tables keep their 32-bit
column. The concurrent index build waits for transactions already open in the database (it does not block
writers); a session left `idle in transaction` delays it. At every start the controller logs a warning for any
sequence above half of its maximum.

### Deprecated values (accepted for this minor release)

An upgrade that reuses a 0.3.x values file keeps working: the chart maps the old keys and lists each one under
**DEPRECATED VALUES** in the install notes. In 0.5 the values schema rejects them.

| 0.3.x value | Mapped to |
|---|---|
| `profile` | ignored (one topology) |
| `replicaCount` | `api.replicas` |
| `resources`, `apiResources` | `controller.resources`, `api.resources` |
| `collector.metricsPort`, `collector.livenessProbe`, `collector.readinessProbe` | `controller.*` |
| `config.api.livenessProbe`, `config.api.readinessProbe` | `api.*`; `config.api.enabled` is ignored (the API serves the UI) |
| `production.components.api.*`, `production.components.controller.*` | `api.*`, `controller.*` (replicas, resources, probe timings) |
| `production.components.automation.*` | `worker.*` |
| `production.leaderElection.*` | `controller.leaderElection.*` (the Lease name is kept) |
| `production.migration.*` | ignored (the controller migrates) |
| `automation.enabled: false`, `automation.mode: disabled` | `automation.policy.pauseDelivery: true` |
| `automation.mode: worker` (or the production default) with automation enabled | `worker.replicas: 1` |
| `automation.workerMetricsPort` | `worker.metricsPort` |
| `rendererSandbox.*` | `renderer.*`; `rendererSandbox.enabled` is ignored |
| `automation.policy.minSamples`, `config.recommendations.verification.minSamples` | still honoured for their gate; prefer `config.recommendations.minSamples` |
| `config.recommendations.sourcePriority` | ignored (see [Removed connectors](#removed-connectors-and-consensus-policies)) |
| `config.recommendations.karpenterEnabled` | ignored: the Karpenter NodePool source was removed (see [Removed advisory analyzers](#removed-advisory-analyzers)) |
| `config.recommendations.zombieCostThreshold`, `zombieEnergyThreshold`, `autoscalingCvThreshold`, `autoscalingSpikeRatio`, `offPeakIdleThreshold`, `offPeakMinIdleHours`, `idleNamespaceEnergyThreshold`, `carbonAwareThreshold`, `nodeUtilizationThreshold` | ignored: their analyzers were removed (see [Removed advisory analyzers](#removed-advisory-analyzers)) |
| `sources.settings` of `greenkube.zombie`, `greenkube.autoscaling`, `greenkube.off_peak`, `greenkube.idle_namespace`, `greenkube.carbon_aware`, `greenkube.nodes`, `greenkube.nodepool` | ignored: these sources were removed (see [Removed advisory analyzers](#removed-advisory-analyzers)) |
| `config.db.type: postgres`, `config.db.path` | ignored |
| `config.db.type: sqlite` | the upgrade fails with the migration commands, see below |

### Removed connectors and consensus policies

GreenKube's measured value now decides every size; VPA, Goldilocks, KRR and pushed signals move the confidence up or
down, and a source alone never creates a recommendation. What was removed, and what happens to an existing install:

| Removed | On upgrade |
|---|---|
| The Kubecost, GKE recommender, Azure Advisor and Polaris connectors | Their `SOURCES_CONFIG` / `sources.settings.<id>` entries (`kubecost`, `gke`, `azure_advisor`, `polaris`) are accepted and ignored, with one warning at start and a line under DEPRECATED VALUES. Push their advice to `POST /api/v1/signals` instead ([Push API](sources/push.md)). |
| `POST /api/v1/sources/polaris/reports` | Answers `404` for every key, a leftover CronJob's `signals:report` or `signals:write` key included; nothing is stored. Delete the job. |
| The chart's KRR and Polaris CronJobs (`sources.krr.job.*`, `sources.polaris.job.*`) | Ignored with a line under DEPRECATED VALUES; the CronJobs, their ServiceAccounts and ClusterRoles are deleted by the upgrade. Run KRR yourself and push its report: [Robusta KRR](sources/krr.md) has a ready-made CronJob. |
| `RECOMMENDATION_CONSENSUS_POLICY` (`safest`, `median`, `weighted`, `priority`; Helm `config.recommendations.consensusPolicy`) | Accepted and ignored, with one warning at start (and a line under DEPRECATED VALUES). |
| `RECOMMENDATION_SOURCE_PRIORITY` (Helm `sources.priority`, `config.recommendations.sourcePriority`) | Accepted and ignored, with one warning at start (and a line under DEPRECATED VALUES). |

Migration `0310` retires the **active** recommendations whose chosen source was one of the removed connectors: they
become `stale` with a `stale` event of reason `type_removed`. Recommendations in a pull request, being measured or
already in the ledger are untouched, and the ledger is never rewritten. The signals, runs and inbox rows of the
removed connectors stay in their tables (nothing writes them any more); a later release drops what is left, after a
backup note here. A recommendation stored before keeps the consensus `policy` it was built with; the next scan
rebuilds it with the native value, which may change its proposed value (a new revision).

These values are refused in a later release: remove them from your values file.

Intentional references to the removed names stay in the code (the B.0 grep exceptions):
`greenkube.core.optimization.eligibility._PUSHED_SOURCE_IDS` keeps `polaris`, so votes of records stored before the
removal still count as pushed (one independent vote); the removed-settings and removed-source lists
(`REMOVED_SETTINGS` in `greenkube.core.config`, `REMOVED_SOURCE_IDS` in `greenkube.signals.registry`, the chart's
deprecation notes and migration `0310`) name them to ignore and warn about them; and `kubecost` as a cost provenance
(`cost_source`) and a protected namespace is OpenCost-compatible cost data, not the removed connector.

Note that `automation.enabled: false` now means *discovery, previews and measurement run, no pull request is
opened*. To keep the Git tokens out of every pod, also remove `automation.providers` and do not create
`greenkube-git-credentials`.

### Removed advisory analyzers

The native advisory analyzers (zombie pods, idle namespaces, off-peak scaling, autoscaling candidates,
carbon-aware scheduling, over- and under-provisioned nodes) and the Karpenter NodePool heuristic are gone: their
advice could not become a pull request or be measured. Rightsizing and the orphaned LoadBalancer and
PersistentVolume detection stay. On upgrade:

- migration 0300 marks the open recommendations of the retired types (`ZOMBIE_POD`, `IDLE_NAMESPACE`,
  `OFF_PEAK_SCALING`, `AUTOSCALING_CANDIDATE`, `CARBON_AWARE_SCHEDULING`, `OVERPROVISIONED_NODE`,
  `UNDERUTILIZED_NODE`), active or ignored, `stale` with a `stale` event of reason `type_removed`. No row is
  deleted, applied and measured ones keep their status, and the savings ledger is not touched;
- `RECOMMENDATION_KARPENTER_ENABLED` and the analyzer thresholds (`ZOMBIE_COST_THRESHOLD`,
  `ZOMBIE_ENERGY_THRESHOLD`, `AUTOSCALING_CV_THRESHOLD`, `AUTOSCALING_SPIKE_RATIO`, `OFF_PEAK_IDLE_THRESHOLD`,
  `OFF_PEAK_MIN_IDLE_HOURS`, `IDLE_NAMESPACE_ENERGY_THRESHOLD`, `CARBON_AWARE_THRESHOLD`,
  `NODE_UTILIZATION_THRESHOLD`) are accepted and ignored for this release, with one warning at start; their Helm
  values are no longer rendered and the notes name them;
- `sources.settings` entries of the removed sources are accepted and ignored, named in the same warning.

The Karpenter, Cluster Autoscaler and HPA reads stay: the realization factor and the rightsizing evidence use them.

### From a standalone or production install with PostgreSQL

Use `--reset-then-reuse-values` (Helm 3.14 or later) or pass your values file again: the new chart defaults
then apply, plus only the values you set. Plain `--reuse-values` does not work for this upgrade: Helm then
keeps the 0.3.x chart's defaults instead of the new ones, so the keys added in 0.4 (`api`, `controller`,
`renderer`, `sources`...) are missing, and the chart stops before changing anything with
`the chart defaults are missing` and these two commands.

```bash
helm repo update
helm upgrade greenkube greenkube/greenkube -n greenkube --reset-then-reuse-values --wait --timeout 10m
kubectl -n greenkube rollout status deployment/greenkube-controller
kubectl -n greenkube rollout status deployment/greenkube-api
```

Helm replaces the standalone Deployment `<release>` (or the production `<release>-automation`) with the new
components (checked on kind by `make upgrade-test`: a seeded 0.3.1 standalone release reached Ready, schema 37
to 99, every row and the API key preserved); the StatefulSet `<release>-postgres`, its claim
`data-<release>-postgres-0` and the Secret `<release>` keep their names. The PostgreSQL pod restarts once (its
image is now pinned by digest) and the controller is recreated rather than rolled (`controller.strategy:
Recreate`), so the old and the new code never write side by side while the schema migrates. The old hook ConfigMap `<release>` is no longer used; delete it once the upgrade succeeded:

```bash
kubectl -n greenkube delete configmap greenkube --ignore-not-found
```

Then move your values to the new keys at your pace (the install notes list what to change). Explicit
`automation.providers` keep working; a provider without `repositoryAllowlist` now uses
`automation.repositoryAllowlist`.

### From a SQLite install

The chart no longer runs SQLite, and an upgrade with `config.db.type=sqlite` fails before Helm changes anything,
printing these steps. Your data stays on the claim `<release>` (it was mounted at `/data`).

1. Switch to the bundled PostgreSQL, keeping the old claim and the controller stopped:

   ```bash
   helm upgrade greenkube greenkube/greenkube -n greenkube --reset-then-reuse-values \
     --set config.db.type=postgres --set controller.replicas=0
   ```

   While `config.persistence.enabled=true` stays set, the chart keeps rendering the old claim with
   `helm.sh/resource-policy: keep`, so nothing deletes it.

2. Copy the data with the one-shot Job below. `greenkube db copy` migrates a working copy of the SQLite file to
   the current schema (the source file is never written), then copies recommendations, their events, the
   savings ledger, change measurements and the GitOps state (providers, bindings, plans, pull requests) into
   PostgreSQL. Add `--include-metrics` to copy the metric history too (large; otherwise it is re-collected).
   Ids are copied as they are, so the copy refuses a target whose tables already hold rows the source also has
   (a controller that already ran against it): copy into the fresh, empty database. To resume an interrupted copy
   into the same database, rerun with `--allow-nonempty`; rows already present are skipped.

   ```yaml
   # greenkube-db-copy.yaml — kubectl -n greenkube apply -f greenkube-db-copy.yaml
   apiVersion: batch/v1
   kind: Job
   metadata:
     name: greenkube-db-copy
   spec:
     backoffLimit: 0
     template:
       metadata:
         labels:
           # Allowed through the bundled PostgreSQL NetworkPolicy (postgres.networkPolicy).
           greenkube.cloud/postgres-client: "true"
       spec:
         restartPolicy: Never
         securityContext:
           runAsNonRoot: true
           runAsUser: 10001
           fsGroup: 10001
         containers:
           - name: copy
             image: greenkube/greenkube:0.4.0   # the version you upgrade to
             command: ["greenkube", "db", "copy",
                       "--from", "sqlite:///data/greenkube_data.db",
                       "--to", "$(DB_CONNECTION_STRING)",
                       "--work-dir", "/work"]
             env:
               - name: DB_CONNECTION_STRING
                 valueFrom:
                   secretKeyRef:
                     name: greenkube          # or greenkube-values / your secrets.existingSecret
                     key: DB_CONNECTION_STRING
             volumeMounts:
               - { name: data, mountPath: /data, readOnly: true }
               - { name: work, mountPath: /work }
         volumes:
           - name: data
             persistentVolumeClaim:
               claimName: greenkube            # the former SQLite claim, named after the release
           - name: work
             emptyDir: {}
   ```

   ```bash
   kubectl -n greenkube wait --for=condition=complete job/greenkube-db-copy --timeout=30m
   kubectl -n greenkube logs job/greenkube-db-copy
   ```

   If your release used `config.db.path`, replace `/data/greenkube_data.db` with it. The working copy needs
   free space about the size of the SQLite file in `/work`. If the 0.3.x pod was killed without a clean shutdown,
   `greenkube_data.db-wal` and `-shm` files sit next to the database and SQLite needs write access to read them:
   if the Job fails with `attempt to write a readonly database` or `unable to open database file`, drop
   `readOnly: true` from the `data` mount (the source file is still never modified by the copy itself).

3. Start the controller:

   ```bash
   helm upgrade greenkube greenkube/greenkube -n greenkube --reset-then-reuse-values --set controller.replicas=1
   ```

4. Once you have checked the data, drop the old claim and the deprecated values:

   ```bash
   kubectl -n greenkube delete job greenkube-db-copy
   kubectl -n greenkube delete pvc greenkube
   helm upgrade greenkube greenkube/greenkube -n greenkube --reset-then-reuse-values \
     --set config.persistence.enabled=false
   ```

The same command works outside Kubernetes, for example to move a local development database:

```bash
greenkube db copy --from sqlite:///path/to/greenkube_data.db --to postgresql://user:pass@host:5432/greenkube
```

### Authentication

Existing users go through the initial-admin flow on their first visit: sign in as `admin` with the password of
the Secret `<release>-initial-admin` and choose a new one. Existing API keys keep working, including the
deprecated bootstrap key `GREENKUBE_API_KEY`, which stays an admin key (`auth.legacyApiKey: admin`).

A 0.3.1 install in `config.apiAuthMode: api_key` has no accounts and no `<release>-initial-admin` Secret. Its
sign-in page asks for an API key: enter `GREENKUBE_API_KEY` from the Secret `<release>` (or a scoped `gk_` key) to
keep browser access ([Browser sign-in with an API key](authentication.md#browser-sign-in-with-an-api-key)). This
needs HTTPS (an Ingress with TLS) or `kubectl port-forward` to `localhost`: plain HTTP to a remote host is refused.
To give
people their own accounts and roles instead, move to `config.apiAuthMode: session`: the upgrade creates the
`<release>-initial-admin` Secret for the first sign-in, and the existing key keeps working for scripts
(`auth.legacyApiKey: admin`).

The upgrade hook adds the scrape token `GREENKUBE_METRICS_TOKEN` to the Secret `<release>` before the Pods roll,
and the ServiceMonitors switch to it in the same release, so an existing Prometheus scrape keeps working. With
`secrets.existingSecret`, the ServiceMonitors keep using its `GREENKUBE_API_KEY` until you add
`GREENKUBE_METRICS_TOKEN` to it and set `secrets.existingSecretHasMetricsToken: true`. Then move scripts to scoped
keys and set `auth.legacyApiKey: metrics` or `disabled`
([The bootstrap API key](authentication.md#the-bootstrap-api-key)).

The bundled PostgreSQL now accepts connections only from the api, controller and worker Pods
(`postgres.networkPolicy`). A Job of yours that connects to it (backup, `greenkube db copy`) needs the label
`greenkube.cloud/postgres-client: "true"` on its Pod template, or a peer in
`postgres.networkPolicy.additionalIngress`.

### ServiceMonitors and the scrape token

Prometheus Operator reads a ServiceMonitor's bearer token Secret in the ServiceMonitor's own namespace. 0.3.x put
the ServiceMonitor in `monitoring` by default but its Secret in the release namespace, so unless both were the same
namespace (or you copied the Secret by hand), Prometheus rejected it with `secret not found` and had no GreenKube
target. `monitoring.serviceMonitor.namespace` now defaults to empty, the release namespace
([Monitoring](configuration.md#monitoring-monitoring)):

- **You kept the default.** The upgrade deletes the ServiceMonitors in `monitoring` and creates them in the release
  namespace, next to the scrape token. Check that Prometheus selects that namespace
  (`serviceMonitorNamespaceSelector`; kube-prometheus-stack selects every namespace by default) and that
  `monitoring.serviceMonitor.additionalLabels` still matches its `serviceMonitorSelector`. Prometheus Operator
  cannot select it? Set `monitoring.serviceMonitor.namespace: monitoring` instead: the upgrade hook then copies only
  the scrape token into Secret `<release>-scrape-token` there.
- **You set the namespace explicitly.** The ServiceMonitors stay where they are and switch to the copy
  `<release>-scrape-token` (key `token`) in that namespace. With `secrets.existingSecret`, the copy needs
  `GREENKUBE_METRICS_TOKEN` in it and `secrets.existingSecretHasMetricsToken: true`; without them nothing is copied
  (never the admin key), and the install notes say which Secret to create.
- **You copied the Secret `<release>` into the ServiceMonitor namespace by hand.** It is no longer referenced and
  holds the bootstrap key and the database password: delete it after the upgrade
  (`kubectl -n monitoring delete secret <release>`).
- **The `prometheus-k8s` RoleBinding** bound the Prometheus ServiceAccount in the ServiceMonitor namespace. It now
  uses `monitoring.serviceMonitor.prometheusNamespace`, else `monitoring.networkPolicy.prometheusNamespace`
  (default `monitoring`). If your Prometheus runs elsewhere, set `monitoring.serviceMonitor.prometheusNamespace`.

After the upgrade, `kubectl get servicemonitors -A -l app.kubernetes.io/name=greenkube` lists them, and the
Prometheus targets page shows `serviceMonitor/<namespace>/<release>/0` up.

### Prometheus metrics, sustainability score and Grafana dashboard

`/prometheus/metrics` now exports GreenKube's own health and the savings ledger counters only. Per-pod and
per-namespace footprint series are gone: on a 40-pod test fixture the API endpoint drops from 75 metric families and
3,066 series (680 with a `pod` label, 2,321 with a `namespace` label) to 7 families and 27 series. Footprint data stays
in the Footprint pages, `GET /api/v1/report/summary`, `GET /api/v1/report/export` and `GET /api/v1/nodes`.
No HTTP API route is removed, no setting or Helm value changes, and no data is deleted: the score was never stored.

**Removed series.** Recording rules, alerts or dashboards of your own that read them return no data after the upgrade:

| Removed | Replacement |
|---|---|
| `greenkube_sustainability_score`, `greenkube_sustainability_dimension_score` | None: the score had no defined method (audit API-06) |
| `greenkube_pod_*` (cost, CO₂e, embodied CO₂e, energy, CPU and memory requests, usage and efficiency, network, disk, restart count), `greenkube_pue`, `greenkube_grid_intensity_gco2_kwh` | Footprint pages and `GET /api/v1/report/export`; usage from your Prometheus (cAdvisor, kube-state-metrics) |
| `greenkube_namespace_*` and `greenkube_cluster_*` footprint totals, `greenkube_cluster_idle_*`, `greenkube_cluster_with_idle_*`, `greenkube_cluster_pod_count`, `greenkube_cluster_namespace_count` | `GET /api/v1/report/summary`, `GET /api/v1/metrics/summary` |
| `greenkube_node_info`, `greenkube_node_cpu_capacity_millicores`, `greenkube_node_memory_capacity_bytes`, `greenkube_node_cpu_allocated_millicores`, `greenkube_node_memory_allocated_bytes`, `greenkube_node_embodied_emissions_kg` | `GET /api/v1/nodes` (Footprint, nodes); kube-state-metrics |
| `greenkube_carbon_intensity_score`, `greenkube_carbon_intensity_zone`, `greenkube_zone_grid_intensity_gco2_kwh` | The Footprint report |
| `greenkube_dashboard_summary_*` | `GET /api/v1/metrics/dashboard-summary` |
| `greenkube_dashboard_savings_co2e_grams_total`, `greenkube_dashboard_savings_cost_dollars_total`, `greenkube_dashboard_savings_accrued_co2e_grams`, `greenkube_dashboard_savings_accrued_cost_dollars` | `increase()` of `greenkube_*_savings_attributed_*_total` minus `greenkube_*_savings_reversed_*_total` over the window, filtered on `method` |
| `greenkube_savings_run_rate_annual_co2e_grams`, `greenkube_savings_run_rate_annual_cost_dollars`, `greenkube_cluster_co2e_saved_grams_total`, `greenkube_cluster_cost_saved_dollars_total` | `GET /api/v1/recommendations/savings` |
| `greenkube_recommendations_total`, `greenkube_recommendations_implemented_total`, `greenkube_recommendations_savings_co2e_grams`, `greenkube_recommendations_savings_cost_dollars`, `greenkube_namespace_recommendation_savings_co2e_grams_total`, `greenkube_namespace_recommendation_savings_cost_dollars_total`, `greenkube_top_recommendations` | `GET /api/v1/recommendations`; scan health from `greenkube_optimization_runs_total` and `greenkube_recommendations_generated_total` |
| `greenkube_estimated_metrics_ratio` | `greenkube_metric_rows_written_total{source}` (controller) |

Kept: `greenkube_last_collection_timestamp_seconds`, `greenkube_metrics_total`, the four ledger counters
`greenkube_{co2e,cost}_savings_{attributed,reversed}_*_total`, `greenkube_savings_measured_vs_projected_ratio`, and
every control-loop family. The chart PrometheusRule reads none of the removed series.

**Grafana.** `dashboards/greenkube-grafana.json` (uid `greenkube-fingreenops`, also published on Grafana.com as
dashboard 25377) is removed, with its generator `scripts/build_grafana_dashboard.py`. Import
`dashboards/greenkube-operations.json` (**GreenKube Operations**, the new revision of Grafana.com dashboard 25377) instead and delete the old dashboard from Grafana:
its panels show "No data" after the upgrade ([Grafana dashboard](prometheus-grafana.md#4-grafana-dashboard)).

### Checking an upgrade without a cluster

The repository renders the 0.3.1 chart and the current chart with the same values and compares what must
survive (PostgreSQL StatefulSet, claim, Service and Secret names, the Lease, selectors, the SQLite claim):

```bash
uv run pytest tests/helm/test_helm_topology.py
```

`tests/integration/test_upgrade_from_v031.py` seeds a schema at migration 0037 (the last 0.3.1 migration) with
`hack/upgrade/seed-v0.3.1.sql` and applies every current migration (needs `GREENKUBE_TEST_POSTGRES_DSN`).

### Checking an upgrade on kind

`make upgrade-test` (`hack/upgrade/upgrade-test.sh`) runs the real upgrade on a throwaway kind cluster, with its
own kubeconfig (your current kubectl context is never used): it installs the 0.3.1 chart and image, seeds the
database, runs `helm upgrade --reset-then-reuse-values --wait` to the working tree with an image built from it,
then checks that the api and the controller are Ready, the schema is at the latest migration, every seeded row
is kept and the API key is unchanged and still accepted. Finally it rolls back as
[Backup and restore](operations/backup-restore.md#roll-back-an-upgrade) describes (restore of the pre-upgrade
dump, then `helm rollback`) and checks the 0.3.1 schema version, the rows and a Ready 0.3.1 release
(`UPGRADE_ROLLBACK=0` skips it). CI runs it on pull requests that touch migrations or
the chart, and every release tag requires it (`.github/workflows/upgrade-test.yml`). Set `UPGRADE_KEEP=1` to
keep the cluster for inspection; the evidence (steps, row counts, logs on failure) lands in
`$TMPDIR/greenkube-upgrade-test/evidence`.

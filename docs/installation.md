# Installation Guide

One `helm install`, one sign-in, optionally one Secret for Git. There is no profile to choose and no feature flag
to flip: GreenKube deploys the full loop and every capability activates itself when its prerequisites appear.
Everything after [Install](#install) is optional and additive.

## Install

You need a Kubernetes cluster you administer, Helm 3.10+ and `kubectl`. Prometheus is strongly recommended
(GreenKube discovers it in the cluster).

```bash
helm repo add greenkube https://GreenKubeCloud.github.io/GreenKube && helm repo update
helm install greenkube greenkube/greenkube --namespace greenkube --create-namespace
```

**1. Sign in.** Open the dashboard and sign in as `admin` with the initial password (you choose a new one at the
first sign-in; your browser can save it):

```bash
kubectl -n greenkube port-forward svc/greenkube-api 8000:8000   # then open http://localhost:8000
kubectl -n greenkube get secret greenkube-initial-admin -o jsonpath='{.data.password}' | base64 -d; echo
```

**2. Connect Git (optional).** GitOps discovery already runs. To let GreenKube open pull requests against the
repositories your Argo CD or Flux sources point at, create one Secret. Delivery activates within a minute,
with no upgrade and no restart:

```bash
kubectl -n greenkube create secret generic greenkube-git-credentials \
  --from-literal=token=<token with write access to the repositories> \
  --from-literal=host=github.com   # the one Git host this token belongs to
```

The token is only ever sent to the host named by `host` (`apiBaseUrl` is accepted too). Without that key it is
used only when every discovered repository lives on `github.com`, or every one on `gitlab.com`. It is never sent to
a self-hosted host the key does not name, so a GitOps source pointing at a server someone else controls cannot
receive it. Repositories on other hosts need an explicit provider ([Automation](automation.md#configure-a-provider)).

**3. Done.** The **Setup** page (`/setup`, or `GET /api/v1/setup/status`) shows what is active and, for anything
missing, the exact fix. Recommendations appear once enough evidence is collected (about 3 hours with the default
5-minute step); the checklist shows the estimate.

## What gets installed

| Component | Default | Responsibilities |
|---|---|---|
| `greenkube-api` Deployment | 1 replica, 100m / 128Mi | REST API, dashboard, webhooks, sign-in. No background job. Ready once the database schema is current. |
| `greenkube-controller` Deployment | 1 replica, 100m / 256Mi | Every periodic loop: collection, scans, measurement, GitOps discovery and sync, automation readiness, plan execution. Migrates the database at startup. Leader election through the `greenkube-leader` Lease. |
| `greenkube-renderer` Deployment | 1 replica, 25m / 96Mi | Isolated Helm/Kustomize rendering: no Secret, no service-account token, egress-deny NetworkPolicy. |
| `greenkube-postgres` StatefulSet | 100m / 128Mi, 10 GiB volume | Bundled PostgreSQL. |
| `greenkube-worker` Deployment | 0 replicas | Optional extra plan executors (`worker.replicas`). |

Requests total about 325m CPU and 600 MiB, small enough for kind or minikube. The chart also creates read-only
cluster RBAC (nodes, pods, workloads, Services, volumes, Argo CD and Flux objects; never Secrets), a namespaced
Role for the Lease, and the credentials bootstrap hook described in [Credentials](#credentials-and-gitops-argo-cd-flux).

What activates when:

| Capability | Active when | Otherwise |
|---|---|---|
| Observe, recommend, measure | Always (each recommendation source activates when its probe succeeds) | — |
| Discover | Argo CD or Flux CRDs are present (re-checked every discovery interval) | Dormant; the checklist explains how to connect a GitOps controller |
| Preview | A discovered, allowlisted repository with credentials and a resolved workload binding | The recommendation shows the blocker and its fix |
| Deliver (open PRs) | Preview + credentials + `automation.policy.pauseDelivery: false` | Dormant with its reason |
| Webhooks | A provider webhook Secret is configured | Polling only |

A pull request is never opened without the approval of a plan digest by a signed-in user.

## Optional: configure your installation

Create a values file instead of `--set` flags, then `helm upgrade greenkube greenkube/greenkube -n greenkube -f my-values.yaml`:

```yaml
# my-values.yaml
config:
  clusterName: prod-eu-west
  cloudProvider: aws          # aws, gcp, azure, ovh, scaleway, on-prem, unknown
  defaultZone: FR             # grid zone when node labels do not identify it
  prometheus:
    url: ""                   # empty: in-cluster discovery
secrets:
  electricityMapsToken: ""    # live grid carbon intensity (optional)
```

The values most often changed:

| Value | Default | Purpose |
|---|---|---|
| `api.replicas`, `controller.replicas` | `1`, `1` | High availability (see [HA](#optional-high-availability)). |
| `api.resources`, `controller.resources`, `renderer.resources` | see above | Component sizing. The controller's memory grows with the number of pods scanned. |
| `image.tag` / `image.digest` | chart app version / `""` | Pin a release, or the exact bytes with `sha256:<64 hex>` (never put a digest in `image.tag`). |
| `config.prometheus.url`, `config.opencost.url` | `""` | Empty attempts in-cluster discovery. |
| `config.recommendations.minSamples` | `36` | Evidence required by every sample gate (recommendations, plan policy, verification). |
| `automation.repositoryAllowlist` | `["@discovered"]` | Repositories PRs may target; an explicit list narrows it. |
| `automation.policy.pauseDelivery` | `false` | Change freeze: no PR is opened, everything else keeps running. |
| `monitoring.serviceMonitor.enabled` | `false` | Prometheus Operator ServiceMonitors for the API and controller metrics. |

See the [Configuration reference](configuration.md) for every value, and [GitOps automation](automation.md) for
providers, policy and webhooks. The chart ships a `values.schema.json`: invalid types and ranges are rejected
before anything is applied.

## Optional: connect Git

The Secret of [step 2](#install) is all most installations need. GreenKube infers the provider of each
discovered repository host: `github.com` is GitHub, `gitlab.com` is GitLab, and a self-hosted host is identified
by probing `/api/v4/version` (GitLab) or `/api/v1/version` (Gitea). The Secret is read for every inferred
provider (key `token`). Delete it and delivery becomes dormant again; open pull requests stay tracked through
webhooks or live-state apply detection.

By default every repository referenced by a discovered Argo CD or Flux source is allowed (`@discovered`). Narrow
it in values, or allow repositories one by one from the setup checklist (stored in the database, applied within
a minute):

```yaml
automation:
  repositoryAllowlist:
    - acme/gitops
    - acme/platform-*
```

Declare a provider explicitly only for several credentials, a self-hosted URL that cannot be probed, or a
separate Secret; explicit entries win over inference:

```yaml
automation:
  providers:
    - id: gitlab-internal
      type: gitlab
      apiBaseUrl: https://git.example.com/api/v4
      secretName: gitlab-token
      secretKey: token
      repositoryAllowlist: ["platform/*"]   # optional, defaults to automation.repositoryAllowlist
```

## Optional: external PostgreSQL

```yaml
postgres:
  enabled: false
secrets:
  dbConnectionString: postgresql://greenkube:<password>@db.example.com:5432/greenkube
config:
  db:
    sslMode: require
```

With `secrets.existingSecret`, put `DB_CONNECTION_STRING` in that Secret instead. The controller creates and
migrates the schema (`config.db.schema`, default `public`) under a PostgreSQL advisory lock. SQLite is for
`greenkube demo` and local development only; the chart no longer offers it (see
[Upgrading](upgrading.md#from-a-sqlite-install)).

## Optional: Ingress and TLS

Route one hostname to the API Service; the dashboard and the API share the origin, so no CORS is needed:

```text
https://greenkube.example.com/   ->  greenkube-api:8000
```

The proxy must preserve the `/api` prefix and forward the `Authorization`, `Cookie`, `Origin` and `X-CSRF-Token`
headers. Serve remote access over HTTPS: the session cookie travels with every request. When TLS terminates at
the Ingress controller, set `config.trustedProxyIps` to its addresses so GreenKube marks the cookie `Secure`, or
set `auth.session.secureCookie: always`. Set `config.externalUrl` for links in pull requests, and
`config.rootPath` when served under a sub-path.

The chart can create the route for you (roadmap N6); the first host also becomes the base of the links in pull
requests (`GREENKUBE_EXTERNAL_URL`) unless `config.externalUrl` is set:

```yaml
# Ingress
ingress:
  enabled: true
  className: nginx
  hosts: [greenkube.example.com]
  tls:
    - secretName: greenkube-tls
      hosts: [greenkube.example.com]
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt

# or a Gateway API HTTPRoute attached to an existing Gateway
httpRoute:
  enabled: true
  parentRefs:
    - name: shared-gateway
      namespace: gateway-system
  hostnames: [greenkube.example.com]
```

See [Exposure](configuration.md#exposure-ingress-httproute) for every key.

## Pod Security Admission

Every pod the chart renders by default (api, controller, renderer, bundled PostgreSQL, credentials bootstrap Job, Helm
test pod, and the optional worker and KRR CronJob) meets the Kubernetes Pod Security Standard `restricted`: non-root,
no privilege escalation, all capabilities dropped and `RuntimeDefault` seccomp. The bundled PostgreSQL needs no root
init container. You can install into a namespace labelled `pod-security.kubernetes.io/enforce=restricted`; CI checks
it with a server-side dry run on kind (roadmap N5).

One option is outside `restricted`: `renderer.egressFirewall.enabled=true` adds an init container that runs as root with
`NET_ADMIN` to install a deny-all egress firewall. It is off by default and only meant for CNIs that do not enforce
NetworkPolicy; keep it off in a `restricted` namespace and rely on the renderer's egress NetworkPolicy, or label the
namespace `baseline`/`privileged` if you need it.

## Optional: high availability

High availability is two numbers, not another install:

```yaml
api:
  replicas: 2
controller:
  replicas: 2       # extra replicas are hot standbys: the Lease hands over within its duration (30 s)
```

API replicas are stateless and share sessions through PostgreSQL. Only the controller holding the
`greenkube-leader` Lease runs the loops; a stopping leader releases the Lease once its loops have stopped, so a standby
takes over at once instead of after its expiry. For very large installations, `worker.replicas` adds dedicated plan executors next
to the controller.

With more than one replica the chart also renders, for the api and the controller:

- a PodDisruptionBudget with `minAvailable: 1` (`api.podDisruptionBudget`, `controller.podDisruptionBudget`), so a
  node drain never evicts every replica at once;
- a soft pod anti-affinity on `kubernetes.io/hostname` (`podAntiAffinity: soft`; `hard` requires distinct nodes,
  `none` turns it off; an explicit `affinity` replaces it).

The controller (and the optional worker) use the `Recreate` strategy: during an upgrade the old pods stop before
the new controller migrates the schema, so two versions never write side by side. The API rolls as usual.

## Production checklist

The defaults fit an evaluation cluster. Before running GreenKube in production:

| Topic | What to set | Reference |
|---|---|---|
| Backups | `postgres.backup.enabled: true` (daily `pg_dump` to its own volume), plus an off-cluster copy | [Backup and restore](operations/backup-restore.md) |
| Upgrades | Back up first; `helm upgrade --wait --timeout 15m` | [Upgrading](upgrading.md#before-every-upgrade-back-up-the-database) |
| Availability | `api.replicas: 2`, `controller.replicas: 2` (PDBs and anti-affinity follow) | [High availability](#optional-high-availability) |
| Alerts | `monitoring.prometheusRule.enabled: true` with the ServiceMonitor: API down, database unavailable, PostgreSQL down, volume filling up, backup missing, loop and ledger alerts | [Observability](observability-slos.md#operations-alerts) |
| Capacity | Size the data volume for your pod count and retention; expand it before it fills | [Sizing](#sizing) |
| Scheduling | Per component (`api`, `controller`, `worker`, `renderer`, `postgres`, `postgres.backup`): `nodeSelector`, `tolerations`, `affinity`, `topologySpreadConstraints`, `priorityClassName`, `podAnnotations`, `podLabels` | [Configuration](configuration.md#scheduling-and-availability) |
| Private registry | `global.imagePullSecrets`, and mirror the images (helper images are pinned by digest) | [Configuration](configuration.md#scheduling-and-availability) |
| Credentials | `secrets.existingSecret` and `auth.admin.existingSecret` under GitOps | [Credentials](#credentials-and-gitops-argo-cd-flux) |
| Exposure | Ingress or HTTPRoute with TLS (needed for `Secure` session cookies) | [Ingress and TLS](#optional-ingress-and-tls) |

```yaml
# production-values.yaml (example)
api:
  replicas: 2
  priorityClassName: platform-high
controller:
  replicas: 2
postgres:
  persistence:
    size: 50Gi
    storageClassName: gp3     # a class with allowVolumeExpansion: true
  resources:
    requests: {cpu: 250m, memory: 512Mi}
    limits: {cpu: '1', memory: 1Gi}
  backup:
    enabled: true
monitoring:
  serviceMonitor:
    enabled: true
  prometheusRule:
    enabled: true
```

## Sizing

The database grows with the number of pods running at the same time (not with their churn) and with the retention
of the hourly aggregates. Raw samples (every 5 minutes, one row per pod and one per container) are kept
`config.metricsRawRetentionDays` (7 days) and then compressed into hourly rows, kept
`config.metricsAggregatedRetentionDays` (unbounded by default).

Measured on PostgreSQL 18 with the current schema and its indexes, a raw pod row takes about 450 bytes and a raw
container row about 350 bytes; hourly rows are of the same order. Allowing for populated provenance columns,
per-workload rollups, recommendations, changes and the ledger, plan for:

| Per 100 concurrent pods (about 1.5 containers each) | Size |
|---|---|
| Raw samples (7 days, steady state, does not grow) | about 0.3 GB |
| Hourly aggregates, per year of retention | about 1.5 to 2 GB |
| Recommendations, plans, changes, measurements, ledger | tens of MB per year |
| PostgreSQL overhead (WAL up to `max_wal_size`, 1 GB by default; dead rows between autovacuum runs) | about 1.5 GB |

So the default 10 GiB volume holds roughly three years of history for 100 pods, or one year for 300 pods. For
1,000 pods and one year, plan about 25 GiB; for multi-year history, multiply the hourly line by the years kept.
Keep at least 30% free: a migration that backfills a table, and `VACUUM`, temporarily need room. The
`GreenKubeDataVolumeLow` and `GreenKubeDataVolumeFillingUp` alerts watch the volume
(`monitoring.prometheusRule.operations`), and the size of the database is one query away:

```bash
kubectl -n greenkube exec greenkube-postgres-0 -c postgres -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc \
   "SELECT pg_size_pretty(pg_database_size(current_database()))"'
```

**Aggregated retention.** The default stays unbounded (`-1`): a finite default would silently delete the history
of existing installations at upgrade, and the hourly aggregates are what year-over-year reports and
re-measurements read. For large clusters, bound it explicitly, for example `config.metricsAggregatedRetentionDays:
400` (13 months, enough for a year-over-year comparison). The savings ledger, recommendations and measurements
are never pruned by this setting.

**Expand the volume.** The bundled claim `data-<release>-postgres-0` can grow in place when its StorageClass has
`allowVolumeExpansion: true` (`kubectl get storageclass`). A StatefulSet's `volumeClaimTemplates` cannot be
changed, so expand the claim itself, then keep the chart value in step for future installs:

```bash
kubectl -n greenkube patch pvc data-greenkube-postgres-0 \
  -p '{"spec":{"resources":{"requests":{"storage":"50Gi"}}}}'
kubectl -n greenkube get pvc data-greenkube-postgres-0 -w      # wait for the new capacity
# Some drivers finish the filesystem resize only when the pod restarts:
kubectl -n greenkube rollout restart statefulset/greenkube-postgres
```

Changing `postgres.persistence.size` alone has no effect on an existing claim (and `helm upgrade` refuses to
change a StatefulSet's claim template: leave it at the old size, or recreate the StatefulSet object with
`kubectl delete statefulset greenkube-postgres --cascade=orphan` before the upgrade, which keeps the pod and its
volume). Without volume expansion, back up, install on a larger volume and [restore](operations/backup-restore.md#restore).

## Optional: SSO and accounts

Browser users sign in with accounts and server-side sessions; machines use scoped API keys created in
**Settings > API keys** (`read`, `write`, `approve`, `plans:deliver`, `signals:write`, `metrics:read`, `admin`). To provide the first
admin password yourself (GitOps), set `auth.admin.existingSecret`. Single sign-on works through an authentication
proxy (`config.apiAuthMode: proxy`) or native OpenID Connect (`config.apiAuthMode: oidc`), with IdP groups mapped to
the `viewer`, `operator` and `admin` roles. See [Authentication](authentication.md).

The bootstrap API key `GREENKUBE_API_KEY` (Secret `greenkube`) still works as an admin key for one more minor
release (deprecated).

## Credentials and GitOps (Argo CD, Flux)

The chart never generates a credential at render time (no `lookup`, no random function), so `helm template`
output is stable under Argo CD and Flux. Credentials come from three places:

| Source | Secret | Managed by |
|---|---|---|
| `secrets.existingSecret` | your Secret | you (recommended for GitOps: External Secrets, Sealed Secrets, SOPS, `kubectl`) |
| Values you set (`secrets.*`, `postgres.auth.password`, service URLs) | `<release>-values` | Helm / Argo CD / Flux |
| Generated when not supplied: `GREENKUBE_API_KEY`, the bundled PostgreSQL password | `<release>` | the `<release>-credentials-bootstrap` hook |

The bootstrap hook (an Argo CD PreSync hook) creates `<release>` only when it is absent and afterwards only adds
missing keys, so it never rotates a value; it also creates `<release>-initial-admin` once. Both survive
`helm uninstall`, like the PostgreSQL volume. It is the only hook of the chart besides the `helm test` pod: the
database migrations run in the controller. For a fully declarative setup, set `secrets.existingSecret` and
`auth.admin.existingSecret`; the chart then renders no Secret and runs no hook.

```bash
kubectl -n greenkube create secret generic greenkube-secrets \
  --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 24)" \
  --from-literal=DB_CONNECTION_STRING="postgresql://greenkube:<same password>@greenkube-postgres:5432/greenkube"
helm upgrade --install greenkube greenkube/greenkube -n greenkube --set secrets.existingSecret=greenkube-secrets
```

The Git credentials Secret (`greenkube-git-credentials`, or `automation.credentialsSecret`) is mounted read-only,
as an optional volume, into the API, the controller and the workers; never into the renderer.

## Upgrades and uninstallation

Upgrading from 0.3.x (profiles, `automation.enabled`, SQLite) is covered in [Upgrading](upgrading.md). Within
0.4, back up the database first ([Upgrading](upgrading.md#before-every-upgrade-back-up-the-database)), then
`helm upgrade` with your values file: the controller migrates the schema at startup and the API replicas report
ready once it is current. To go back after a migration, restore the backup: `helm rollback` alone does not undo
migrations ([Backup and restore](operations/backup-restore.md#roll-back-an-upgrade)).

```bash
helm uninstall greenkube --namespace greenkube
```

The PostgreSQL volume and the generated Secret `greenkube` survive uninstallation, so a reinstall reuses the data
and its password. For a clean reinstall delete both:

```bash
kubectl -n greenkube delete pvc data-greenkube-postgres-0
kubectl -n greenkube delete secret greenkube greenkube-initial-admin
```

If the Secret was deleted but the volume kept, the bootstrap hook refuses to generate a new password (it could
not authenticate against the existing database) and prints the recovery command.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `greenkube-api` stays not ready, `/api/v1/health` returns `migrating` | The controller has not migrated the schema yet. | `kubectl -n greenkube logs deploy/greenkube-controller`; a large upgrade can take minutes. |
| Pods stay in `Init` | The bundled PostgreSQL is not ready. | `kubectl -n greenkube logs <pod> -c wait-for-postgres`; check the PostgreSQL pod and PVC. |
| Controller crash-loops with `password authentication failed` | A PostgreSQL volume from an earlier install was reused with another password. | Delete `data-greenkube-postgres-0`, or put the original password in the Secret `greenkube` or `secrets.existingSecret`. |
| `helm install` fails with a values schema error | A value has the wrong type or is out of range. | Fix the value named in the message; see [Configuration](configuration.md). |
| `helm upgrade` fails with `config.db.type=sqlite is no longer supported` | A 0.3.x SQLite release. | Follow [Upgrading from SQLite](upgrading.md#from-a-sqlite-install). |
| Setup checklist: delivery `credentials_missing` | No Git credentials are mounted. | Create `greenkube-git-credentials` ([Connect Git](#optional-connect-git)); it is read through the Kubernetes API on the next readiness tick (within 60 s). |
| Setup checklist: delivery `credentials_host_ambiguous` or `credentials_host_mismatch` | The token is not bound to a host, and repositories use several hosts or a self-hosted one; or the `host` key names a host no repository uses. | Add (or fix) the `host` key of `greenkube-git-credentials`; declare explicit providers for the other hosts. |
| Setup checklist: `renderer_egress_unenforced` | The network plugin does not enforce NetworkPolicy. | Use a CNI that does, or `--set renderer.egressFirewall.enabled=true`. Previews still work. |
| Dashboard shows no data | Prometheus is unreachable or not discovered. | Set `config.prometheus.url`. |
| API returns `401` / `403 CSRF_REJECTED` | No session, a wrong key, or a proxy that strips headers. | Sign in again; forward `Origin` and `X-CSRF-Token`. |
| Pod in `InvalidImageName` | A digest was passed through `image.tag`. | Use `image.digest=sha256:<64 hex>`. |

## Try the demo

```bash
docker run --rm -p 9000:9000 greenkube/greenkube demo --no-browser --port 9000
# Open http://localhost:9000
```

The demo uses a temporary SQLite database with a two-year sample history.

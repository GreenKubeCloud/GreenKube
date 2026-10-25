# Installation Guide

This guide covers a complete GreenKube installation with the official Helm chart. It explains the two deployment profiles, the values you are most likely to change, how API-key authentication works, and how to reach the dashboard from a workstation.

For the full values reference, see the [Configuration reference](configuration.md). For the optional GitOps workflow, see [GitOps automation](automation.md).

## Before you begin

### Requirements

| Requirement | Details |
|---|---|
| Kubernetes cluster | A cluster you can administer. The chart creates a `ClusterRole`/`ClusterRoleBinding` so GreenKube can read nodes, pods, workloads, and supported GitOps resources. |
| Helm | Helm 3. |
| kubectl | A version compatible with your cluster. |
| Prometheus | Strongly recommended. GreenKube reads CPU, memory, network, disk, and restart metrics from Prometheus. Auto-discovery is attempted when `config.prometheus.url` is empty. |
| Metrics Server | Optional but recommended. When `metrics.k8s.io` is available, GreenKube reads per-container CPU and memory for recommendation evidence and verification. |
| OpenCost | Optional. Provides real cost allocation instead of estimates. |
| Electricity Maps or Wattnet | Optional. Without a carbon-intensity provider, GreenKube falls back to bundled/static intensity data and `config.defaultIntensity`. |
| GitOps controllers | Optional. Argo CD and/or Flux are required only for the automation-backed plan and pull-request workflow. |
| Git provider access | Optional. GitHub, GitLab, or Gitea with a token in a Kubernetes Secret and a repository allowlist. |

### Deployment profiles

The chart has two profiles:

| Profile | Workloads | Use it for |
|---|---|---|
| `standalone` (default) | Collector and API in one Deployment. Automation runs inline when enabled. | Evaluations, small clusters, local installations. |
| `production` | Separate API, controller, and (when automation is enabled) automation worker Deployments. The controller uses a Kubernetes Lease for leader election; database migrations run as a Helm hook Job. | Production clusters and any installation that needs component isolation or horizontal API scaling. |

The default chart values use `profile: standalone`, `config.environment: production`, `config.apiAuthMode: api_key`, and a generated API key. GitOps automation is disabled until you explicitly enable it.

### Development vs production at a glance

| Aspect | Development | Production |
|---|---|---|
| Profile | `standalone` | `production` |
| Workloads | Collector and API in one pod | Separate API, controller, and optional automation worker |
| API authentication | A key is optional when `config.environment=development`; an installation with no key in its Secret is open | `GREENKUBE_API_KEY` is mandatory; missing configuration returns `503` |
| Migrations | Applied by the collector/API at startup | Dedicated post-install/post-upgrade Job |
| API replicas | One | Two by default, horizontally scalable |
| Leader election | Not used | Controller Lease |
| Intended use | Local demos, evaluations, protected clusters | Production clusters |

## Quick start

### 1. Add the chart repository

```bash
helm repo add greenkube https://GreenKubeCloud.github.io/GreenKube
helm repo update
```

### 2. Install GreenKube

```bash
helm upgrade --install greenkube greenkube/greenkube \
  --namespace greenkube \
  --create-namespace
```

This creates:

- the GreenKube API/collector workload,
- a bundled PostgreSQL StatefulSet and Service,
- a Secret containing a generated `GREENKUBE_API_KEY` and database credentials,
- the read-only Kubernetes RBAC required by the collectors and recommendation engine.

### 3. Verify the rollout

```bash
kubectl -n greenkube get pods
kubectl -n greenkube get jobs
kubectl -n greenkube logs deploy/greenkube -c greenkube --tail=100
kubectl -n greenkube logs deploy/greenkube -c greenkube-api --tail=100
```

With the `production` profile, the component pods are named `<release>-api`, `<release>-controller`, and, when automation is enabled, `<release>-automation`. The migration Job must complete before the API is ready.

### 4. Forward the API and dashboard

The chart serves the dashboard and the REST API from the same pod:

```bash
kubectl -n greenkube port-forward svc/greenkube-api 8000:8000
```

Open <http://localhost:8000>.

Service names follow the Helm fullname convention. With the release name `greenkube` above, the Service is `greenkube-api` and the chart-managed Secret is `greenkube`. For a release named `greenkube-prod`, they are `greenkube-prod-api` and `greenkube-prod` unless you set `secrets.existingSecret`.

### 5. Retrieve the API key

In `production` authentication mode the dashboard asks for an API key. Retrieve the generated value from the release Secret:

```bash
kubectl -n greenkube get secret greenkube \
  -o jsonpath='{.data.GREENKUBE_API_KEY}' | base64 -d
```

Paste the value into the dashboard's **API authentication required** dialog and select **Connect**. The browser keeps the key in memory only: it is never stored in `localStorage` or cookies, so a full page reload asks for it again.

For API clients:

```bash
export GREENKUBE_API_KEY="$(kubectl -n greenkube get secret greenkube \
  -o jsonpath='{.data.GREENKUBE_API_KEY}' | base64 -d)"

curl -H "Authorization: Bearer ${GREENKUBE_API_KEY}" \
  http://localhost:8000/api/v1/metrics/summary
```

Never put the key in a URL, a values file committed to Git, or shell history on a shared machine.

## Configure your installation

Create a values file instead of passing dozens of `--set` flags:

```yaml
# my-values.yaml
config:
  clusterName: prod-eu-west
  cloudProvider: aws
  # Optional: override when node labels do not identify the grid zone.
  defaultZone: FR

  prometheus:
    # Empty means in-cluster auto-discovery.
    url: ""
  opencost:
    url: ""

secrets:
  # Optional: real-time grid carbon intensity.
  electricityMapsToken: ""
```

Apply it:

```bash
helm upgrade --install greenkube greenkube/greenkube \
  --namespace greenkube \
  -f my-values.yaml
```

### Pin the image by tag or digest

By default the chart uses the image tag matching its app version. To pin a specific release, use `image.tag`:

```bash
helm upgrade --install greenkube greenkube/greenkube \
  --namespace greenkube \
  --set image.tag=0.3.0
```

To pin the exact image bytes, use `image.digest`. Do **not** put `sha256:` in `image.tag`; that renders an invalid Kubernetes image reference (`greenkube/greenkube:sha256:...`) and the pod stays in `InvalidImageName`.

```bash
helm upgrade --install greenkube greenkube/greenkube \
  --namespace greenkube \
  --create-namespace \
  --set image.digest="sha256:<64-hex-digest>" \
  --wait --timeout 5m
```

Replace `<64-hex-digest>` with the digest reported by your registry or CI. The rendered reference is `greenkube/greenkube@sha256:<digest>`. `image.digest` takes precedence over `image.tag`. You can also pin a custom registry or mirror with `image.repository`.

### Variables you are most likely to change

#### Deployment and image

| Value | Default | Purpose |
|---|---|---|
| `profile` | `standalone` | `standalone` or `production`. See [Deployment profiles](#deployment-profiles). |
| `replicaCount` | `1` | Replicas for the standalone collector/API pod. |
| `image.repository` | `greenkube/greenkube` | Image repository. |
| `image.tag` | chart app version | Image tag. Must be a tag, not a `sha256:` digest. |
| `image.digest` | `""` | Pin the exact image with `sha256:<64 hex>`; takes precedence over `image.tag`. |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `apiResources` | 50m/128Mi requests, 250m/1Gi limits | API container resources. The API builds recommendation inputs during refresh, so keep memory headroom. |
| `resources` | 100m/128Mi requests, 500m/512Mi limits | Collector/worker resources. Memory grows with the number of pods. |
| `tmp.sizeLimit` | `768Mi` | Scratch space for snapshots and renderer streaming. |

#### Runtime configuration

| Value | Default | Purpose |
|---|---|---|
| `config.environment` | `production` | `production`, `development`, or `test`. Production fails closed if authentication is not configured. |
| `config.apiAuthMode` | `api_key` | The only request-authentication implementation currently supported. `oidc`/`session` are configuration contracts and return `503` at request time. |
| `config.clusterName` | `""` | Label used in Prometheus metrics. Empty enables auto-detection from the node/cloud provider. |
| `config.cloudProvider` | `unknown` | `aws`, `gcp`, `azure`, `ovh`, `scaleway`, `on-prem`, or `unknown`. |
| `config.defaultZone` | `""` | Fallback grid zone (for example `FR`, `DE`, `US-CAL-CISO`). |
| `config.defaultIntensity` | `500.0` | Fallback grid intensity in gCO₂e/kWh. |
| `config.electricityProvider` | `electricity_maps` | `electricity_maps` or `wattnet`. |
| `config.normalizationGranularity` | `hour` | `hour`, `day`, or `none`. |
| `config.logLevel` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. |
| `config.logFormat` | `json` | `json` for log pipelines or `console` for human-readable logs. |
| `config.corsOrigins` | `*` | Allowed CORS origins. Leave `*` only when the API is not publicly exposed. |
| `config.rootPath` | `""` | Sub-path when served behind a reverse proxy, for example `/greenkube`. |
| `config.prometheus.url` | `""` | Empty attempts in-cluster discovery. |
| `config.prometheus.queryRangeStep` | `5m` | Prometheus range-query step. Must divide 24 hours. |
| `config.opencost.url` | `""` | Empty attempts in-cluster discovery. |
| `config.kubernetesMetricsApiEnabled` | `true` | Read per-container usage from `metrics.k8s.io` when available. |
| `config.serviceUrlAllowlist` | `[]` | Exact hosts allowed for runtime service-URL overrides from the Settings page. |

#### Database and retention

| Value | Default | Purpose |
|---|---|---|
| `config.db.type` | `postgres` | `postgres` (recommended) or `sqlite` (development only). |
| `postgres.enabled` | `true` | Deploy the bundled PostgreSQL StatefulSet. Set to `false` to use an external database. |
| `secrets.dbConnectionString` | `""` | Required when `postgres.enabled=false` and you use the chart-managed Secret. With `secrets.existingSecret`, put `DB_CONNECTION_STRING` in that Secret. |
| `postgres.persistence.size` | `10Gi` | Bundled PostgreSQL volume size. |
| `config.persistence.enabled` | `false` | Persistent volume for SQLite. Enable it if you use SQLite and want data to survive pod restarts. |
| `config.metricsCompressionAgeHours` | `24` | Age after which raw metrics are compressed to hourly aggregates. |
| `config.metricsRawRetentionDays` | `7` | Raw retention after compression. |
| `config.metricsAggregatedRetentionDays` | `-1` | Hourly retention; `-1` keeps data indefinitely. |

#### Secrets

| Value | Purpose |
|---|---|
| `secrets.existingSecret` | Use a pre-created Secret instead of the chart-managed one. Recommended for production. |
| `secrets.apiKey` | API bearer key. If empty, the chart generates one on first install and preserves it on upgrades. |
| `secrets.electricityMapsToken` | Electricity Maps token. |
| `secrets.wattnetEmail`, `secrets.wattnetPassword` | Wattnet credentials when `config.electricityProvider=wattnet`. |
| `secrets.boaviztaToken` | Optional Boavizta token. |
| `secrets.prometheus.*` | Optional Prometheus basic-auth or bearer token. |

#### Monitoring and automation

| Value | Default | Purpose |
|---|---|---|
| `monitoring.serviceMonitor.enabled` | `false` | Create a Prometheus Operator ServiceMonitor for `/prometheus/metrics`. |
| `monitoring.prometheusRule.enabled` | `false` | Create alerts for stuck automation operations, sync failures, and stale workers. |
| `monitoring.networkPolicy.enabled` | `false` | Restrict component ingress/egress. |
| `automation.enabled` | `false` | Enable GitOps discovery, planning, pull requests, and apply tracking. |
| `automation.mode` | profile-dependent | `inline` for standalone, `worker` for production. |
| `automation.providers` | `[]` | Git provider definitions with Secret references and repository allowlists. |
| `rendererSandbox.enabled` | `true` | Run Helm/Kustomize rendering in an isolated Pod. Required for automation-backed rendering in production. |

See the [Configuration reference](configuration.md) for every value, and [GitOps automation](automation.md) for the complete automation setup.

## Development mode

Use development mode only for local demonstrations or a protected cluster. It is not a security boundary.

```bash
helm upgrade --install greenkube-dev greenkube/greenkube \
  --namespace greenkube-dev \
  --create-namespace \
  --set profile=standalone \
  --set config.environment=development \
  --set config.clusterName=minikube \
  --set config.defaultZone=FR
```

Development mode differences:

- The API can start without an API key if the mounted Secret has no `GREENKUBE_API_KEY`; protected API routes are then open.
- Production configuration would return `503` for the same missing key instead of allowing anonymous access.
- The chart still generates a key by default, so the simplest development setup is to retrieve and use that key exactly like production. This avoids an accidentally unauthenticated service.

To intentionally run with authentication disabled, use a pre-created Secret that has no `GREENKUBE_API_KEY` and, if you use the bundled PostgreSQL, contains the database keys as well:

```bash
kubectl create namespace greenkube-dev --dry-run=client -o yaml | kubectl apply -f -

kubectl -n greenkube-dev create secret generic greenkube-dev-secrets

helm upgrade --install greenkube-dev greenkube/greenkube \
  --namespace greenkube-dev \
  --set config.db.type=sqlite \
  --set config.persistence.enabled=true \
  --set config.environment=development \
  --set secrets.existingSecret=greenkube-dev-secrets
```

With SQLite, no database Secret keys are required. If you keep PostgreSQL, create the Secret with `POSTGRES_PASSWORD` and `DB_CONNECTION_STRING` values (see [Production API-key handling](#production-api-key-handling)).

Access the UI the same way:

```bash
kubectl -n greenkube-dev port-forward svc/greenkube-dev-api 8000:8000
# Open http://localhost:8000
```

Development mode still uses real Prometheus/OpenCost/Kubernetes data. Use the [demo mode](#try-the-demo) when you only want sample data.

## Production mode

### Production values

Start from the chart defaults and override what you need:

```yaml
# production-values.yaml
profile: production

config:
  environment: production
  apiAuthMode: api_key
  clusterName: prod-eu-west
  cloudProvider: aws

production:
  migration:
    enabled: true
  leaderElection:
    enabled: true
  components:
    api:
      replicas: 2
    controller:
      replicas: 1

secrets:
  existingSecret: greenkube-secrets

postgres:
  enabled: true
  persistence:
    enabled: true
    size: 20Gi
```

Install with `production-values.yaml` and, when you are ready, enable automation as described in [GitOps automation](automation.md).

Production mode differences:

- The API and controller run separately; enabling automation adds a dedicated worker Deployment.
- The API can be scaled horizontally; the controller uses the `<release>-leader` Lease so only one controller runs collection, discovery, and sync observation.
- Migrations run as a post-install/post-upgrade Job before the API is considered ready.
- The automation worker is a single replica. It claims queued operations through database leases, so do not scale it manually.
- Production keeps the API key mandatory. A missing key or unavailable authentication configuration returns `503` rather than opening the API.

### Production API-key handling

For production, create the Secret outside Helm and never put credentials in a values file:

```bash
export GREENKUBE_API_KEY="$(openssl rand -hex 32)"
export POSTGRES_PASSWORD="$(openssl rand -hex 24)"

kubectl create namespace greenkube --dry-run=client -o yaml | kubectl apply -f -

kubectl -n greenkube create secret generic greenkube-secrets \
  --from-literal=GREENKUBE_API_KEY="${GREENKUBE_API_KEY}" \
  --from-literal=POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
  --from-literal=DB_CONNECTION_STRING="postgresql://greenkube:${POSTGRES_PASSWORD}@greenkube-postgres:5432/greenkube"
```

The `DB_CONNECTION_STRING` host must match your PostgreSQL Service (`<release>-postgres` for the bundled database). Add `ELECTRICITY_MAPS_TOKEN`, `WATTNET_EMAIL`, `WATTNET_PASSWORD`, `BOAVIZTA_TOKEN`, or Prometheus credentials as needed.

Then reference it:

```yaml
secrets:
  existingSecret: greenkube-secrets
```

and install:

```bash
helm upgrade --install greenkube greenkube/greenkube \
  --namespace greenkube \
  -f production-values.yaml
```

Key-handling rules:

- Store the key in a Secret, a password manager, or your secret-management system. Do not commit it.
- Retrieve it only when needed:
  ```bash
  kubectl -n greenkube get secret greenkube-secrets \
    -o jsonpath='{.data.GREENKUBE_API_KEY}' | base64 -d
  ```
- Rotate by updating the Secret and restarting the API Deployment:
  ```bash
  kubectl -n greenkube create secret generic greenkube-secrets \
    --from-literal=GREENKUBE_API_KEY="$(openssl rand -hex 32)" \
    --from-literal=POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
    --from-literal=DB_CONNECTION_STRING="postgresql://greenkube:${POSTGRES_PASSWORD}@greenkube-postgres:5432/greenkube" \
    --dry-run=client -o yaml | kubectl apply -f -

  kubectl -n greenkube rollout restart deploy/greenkube-api
  ```
- The browser keeps the entered key in memory only; it is not persisted. Users must re-enter it after a reload.
- Run the UI and API on the same origin. If they must differ, set `config.corsOrigins` explicitly and make sure the proxy forwards `Authorization` and `OPTIONS`.

### Reach the frontend

**Local access with port forwarding**

```bash
kubectl -n greenkube port-forward svc/greenkube-api 8000:8000
```

Open <http://localhost:8000>. The API and UI share the origin, so no CORS or frontend build configuration is required. The login dialog appears on the first protected request. Paste the key from `greenkube-secrets`.

**Ingress or Gateway API**

Route the same hostname to the API Service:

```text
https://greenkube.example.com/            -> greenkube-api:8000
https://greenkube.example.com/api/v1/...  -> greenkube-api:8000
```

The proxy must preserve the `/api` prefix, forward the `Authorization` header, and allow `GET`, `POST`, `PATCH`, and `DELETE`. Because the UI and API are same-origin, no CORS configuration is required. If you terminate TLS at the proxy, do not expose the API key in query strings or logs.

If the installation is served under a sub-path, set `config.rootPath` (for example `/greenkube`) and configure the proxy to forward that prefix.

### Database choices in production

- **Bundled PostgreSQL (default):** Simplest. Persistence is enabled by default with a 10 GiB volume. Set a fixed `postgres.auth.password` only when you manage the Secret yourself; otherwise the chart preserves a generated password across upgrades.
- **External PostgreSQL:** Set `postgres.enabled=false`. With the chart-managed Secret, provide `secrets.dbConnectionString`; with `secrets.existingSecret`, put `DB_CONNECTION_STRING` in that Secret. Tune `config.db.sslMode`, `config.db.poolMinSize`, `config.db.poolMaxSize`, and `config.db.statementTimeoutMs`.
- **SQLite:** Acceptable for development only. If you use it, enable `config.persistence.enabled` so the database survives pod restarts.

## Optional: GitOps automation

Automation is disabled by default. To enable it you need:

1. Argo CD and/or Flux controllers that GreenKube can read.
2. A CNI that enforces NetworkPolicy (or a cluster where you have validated the renderer sandbox boundary).
3. A Git provider token in a Kubernetes Secret.
4. A repository allowlist for that provider.

Minimal provider setup:

```bash
kubectl -n greenkube create secret generic greenkube-git \
  --from-file=github-token=./github-token
```

```yaml
automation:
  enabled: true
  mode: worker          # default in the production profile
  providers:
    - id: github-main
      type: github
      apiBaseUrl: https://api.github.com
      secretName: greenkube-git
      secretKey: github-token
      repositoryAllowlist:
        - "acme/gitops"
  policy:
    maxRiskLevel: medium
    minConfidence: 0.6
    minSamples: 36
    minCoverage: 0.5
  pullRequests:
    draft: false
    labels: []
rendererSandbox:
  enabled: true
```

The Secret must be in the Helm release namespace. Credential values are never written to ConfigMaps. Repository allowlists are required; an empty allowlist denies every repository. See [GitOps automation](automation.md) for webhooks, policy, plan lifecycle, rollback, and supported source layouts.

Verify readiness:

```bash
export GREENKUBE_API_KEY="$(kubectl -n greenkube get secret greenkube-secrets \
  -o jsonpath='{.data.GREENKUBE_API_KEY}' | base64 -d)"

curl -H "Authorization: Bearer ${GREENKUBE_API_KEY}" \
  http://localhost:8000/api/v1/automation/status
```

The **Automation & GitOps** page in the dashboard shows controller capabilities, discovery freshness and coverage, blocked/ambiguous workload bindings, and safe pull-request metadata.

## Upgrades and uninstallation

Upgrade with the same values file:

```bash
helm upgrade greenkube greenkube/greenkube \
  --namespace greenkube \
  -f production-values.yaml
```

The chart preserves generated API keys, PostgreSQL passwords, and runtime service overrides that were written to the Secret.

Uninstall:

```bash
helm uninstall greenkube --namespace greenkube
```

The bundled PostgreSQL PVC survives uninstallation. If the chart-managed Secret is missing as well (for example because it was created by an older chart version that tracked it as a regular resource, or because it was deleted manually), a reinstall generates a new password that cannot authenticate against the existing database. The pod then crash-loops with `password authentication failed for user "greenkube"`.

Either delete the volume for a clean install, or provide the existing database password before reinstalling:

```bash
# Clean install (destroys the old data)
kubectl -n greenkube delete pvc data-greenkube-postgres-0
```

```bash
# Keep the data: reuse the old password through an explicit Secret
kubectl -n greenkube create secret generic greenkube-secrets \
  --from-literal=GREENKUBE_API_KEY="$(openssl rand -hex 32)" \
  --from-literal=POSTGRES_PASSWORD="<the-password-used-by-the-existing-database>" \
  --from-literal=DB_CONNECTION_STRING="postgresql://greenkube:<the-password-used-by-the-existing-database>@greenkube-postgres:5432/greenkube"

helm upgrade --install greenkube greenkube/greenkube \
  --namespace greenkube \
  --set secrets.existingSecret=greenkube-secrets
```

The chart rejects a fresh install when a retained PostgreSQL PVC exists without its Secret, instead of starting a database that cannot authenticate.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| Dashboard shows **API authentication required** | The API is protected. | Paste the value of `GREENKUBE_API_KEY` from the release Secret. |
| API returns `401` | Missing or wrong bearer token. | Send `Authorization: Bearer <key>`; re-enter the key in the UI. |
| API returns `503` with "API authentication is not configured" | `config.environment=production` but the Secret has no key. | Create/reference a Secret containing `GREENKUBE_API_KEY`. |
| Dashboard shows no data | Prometheus is unreachable or not discovered. | Open **Settings**, check service health, and set `config.prometheus.url` explicitly. |
| Pod stays in `InvalidImageName` or events show `couldn't parse image name "...:sha256:..."` | A digest was passed through `image.tag`. | Use `--set image.digest=sha256:<64 hex>` instead. `image.repository` must stay a repository name. |
| Collector/API crash-loop with `password authentication failed for user "greenkube"` | A PostgreSQL PVC from an earlier installation was reused with a newly generated password. | Delete `data-greenkube-postgres-0` for a clean install, or set `secrets.existingSecret` (or `postgres.auth.password`) to the password used by the existing database. |
| `helm upgrade` fails with a pre-upgrade hook error for `<release>-db-credential-check` | The chart Secret no longer matches the existing PostgreSQL volume. | Run `kubectl -n greenkube logs job/<release>-db-credential-check`; it prints the recovery steps. Delete the PVC or provide the original password. |
| Pod stays in `Init` waiting for PostgreSQL | The bundled database is not ready. | `kubectl -n greenkube logs <pod> -c wait-for-postgres`; check the PostgreSQL pod and PVC. |
| Migration Job fails | Database credentials or connectivity are wrong. | Read the Job logs; verify `DB_CONNECTION_STRING`, `POSTGRES_PASSWORD`, and the Service name. |
| Automation page reports discovery unavailable | GreenKube cannot read Kubernetes/GitOps APIs or no controllers are installed. | Check RBAC and controller CRDs; run `POST /api/v1/gitops/refresh`. |
| Automation is not ready | Worker is disabled, stale, or the renderer is unavailable. | Check `/api/v1/automation/status`, worker logs, and `rendererSandbox`. |
| No per-container recommendations | Container metrics are unavailable. | Install/enable Metrics Server and verify `metrics.k8s.io`. |

## Try the demo

The Docker image can run a self-contained demo with sample data and no Kubernetes or external services:

```bash
docker run --rm -p 9000:9000 greenkube/greenkube demo --no-browser --port 9000
# Open http://localhost:9000
```

The demo uses a temporary SQLite database generated at startup. It includes a two-year sample history, recommendations, nodes, reports, and dashboard summaries.

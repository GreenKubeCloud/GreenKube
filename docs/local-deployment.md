# Local Minikube deployments

This runbook describes two supported local usage modes. For the full production guide, values reference, and API-key handling, see the [Installation guide](installation.md).

* **Development/demo**: real cluster data with the standalone profile, intended for a local `kubectl port-forward`.
* **Production-like**: the production Helm profile with separate API/controller deployments and mandatory API-key authentication.

Use separate namespaces and releases for both modes. Do not reuse a production Secret for a demo installation.

## Prerequisites

The Minikube Docker daemon must contain the image being deployed:

```bash
eval "$(minikube docker-env)"
docker build -t greenkube/greenkube:local-demo .
```

The examples use the in-cluster Prometheus and OpenCost services. Adjust the URLs if those services have different names in your cluster.

## Development instance

Create a fresh namespace so the release does not reuse an existing generated Secret:

```bash
helm upgrade --install greenkube-local-dev ./helm-chart \
  --namespace greenkube-local-dev \
  --create-namespace \
  --set profile=standalone \
  --set image.repository=greenkube/greenkube \
  --set image.tag=local-demo \
  --set config.environment=development \
  --set config.apiAuthMode=api_key \
  --set config.clusterName=minikube \
  --set config.defaultZone=FR \
  --set config.prometheus.url=http://prometheus-k8s.monitoring.svc.cluster.local:9090 \
  --set config.opencost.url=http://opencost.opencost.svc.cluster.local:9003 \
  --wait
```

The chart still generates an API key by default. Retrieve it and paste it into the dashboard login dialog:

```bash
kubectl -n greenkube-local-dev get secret greenkube-local-dev \
  -o jsonpath='{.data.GREENKUBE_API_KEY}' | base64 -d
```

If you intentionally want an open API in development, use an existing Secret that has no `GREENKUBE_API_KEY` and switch to SQLite so no database keys are needed:

```bash
kubectl -n greenkube-local-dev create secret generic greenkube-local-dev-secrets

helm upgrade --install greenkube-local-dev ./helm-chart \
  --namespace greenkube-local-dev \
  --set image.repository=greenkube/greenkube \
  --set image.tag=local-demo \
  --set config.environment=development \
  --set config.db.type=sqlite \
  --set config.persistence.enabled=true \
  --set secrets.existingSecret=greenkube-local-dev-secrets
```

GreenKube still collects real Prometheus/OpenCost/Kubernetes data. Do not expose an open instance publicly.

Find the API service and forward it locally:

```bash
kubectl -n greenkube-local-dev get svc
kubectl -n greenkube-local-dev port-forward \
  svc/greenkube-local-dev-api 8000:8000
```

Open <http://localhost:8000>. The API and UI use the same origin, so no CORS or browser token configuration is required beyond the login dialog.

## Production-like instance

Generate the API key outside the manifest and create the namespace and Secret:

```bash
kubectl create namespace greenkube-local-prod --dry-run=client -o yaml | kubectl apply -f -

export GREENKUBE_LOCAL_PROD_API_KEY="$(openssl rand -hex 32)"
export GREENKUBE_LOCAL_PROD_DB_PASSWORD="$(openssl rand -hex 24)"

kubectl -n greenkube-local-prod create secret generic greenkube-local-prod-secrets \
  --from-literal=GREENKUBE_API_KEY="${GREENKUBE_LOCAL_PROD_API_KEY}" \
  --from-literal=POSTGRES_PASSWORD="${GREENKUBE_LOCAL_PROD_DB_PASSWORD}" \
  --from-literal=DB_CONNECTION_STRING="postgresql://greenkube:${GREENKUBE_LOCAL_PROD_DB_PASSWORD}@greenkube-local-prod-postgres:5432/greenkube"
```

Install the production-like release:

```bash
helm upgrade --install greenkube-local-prod ./helm-chart \
  --namespace greenkube-local-prod \
  --create-namespace \
  --set profile=production \
  --set image.repository=greenkube/greenkube \
  --set image.tag=local-demo \
  --set config.environment=production \
  --set config.apiAuthMode=api_key \
  --set config.clusterName=minikube \
  --set config.defaultZone=FR \
  --set config.prometheus.url=http://prometheus-k8s.monitoring.svc.cluster.local:9090 \
  --set config.opencost.url=http://opencost.opencost.svc.cluster.local:9003 \
  --set secrets.existingSecret=greenkube-local-prod-secrets \
  --wait
```

Forward the production-like UI:

```bash
kubectl -n greenkube-local-prod port-forward \
  svc/greenkube-local-prod-api 8000:8000
```

Open <http://localhost:8000> and paste `GREENKUBE_LOCAL_PROD_API_KEY` into the authentication dialog. The frontend keeps the key in memory only and sends it as a Bearer token.

For API clients:

```bash
curl \
  -H "Authorization: Bearer ${GREENKUBE_LOCAL_PROD_API_KEY}" \
  http://localhost:8000/api/v1/metrics/summary
```

Do not print the key or commit it to a values file. If the shell variable is lost, retrieve the value from the Kubernetes Secret using the normal Secret access procedure.

The production profile runs the migration Job automatically. Watch it before using the instance:

```bash
kubectl -n greenkube-local-prod get jobs -w
```

## Ingress and HTTPRoute

Port-forwarding does not require an Ingress or HTTPRoute. For a shared local or remote URL, route the UI and API through the same hostname:

```text
https://greenkube.example.test/            -> greenkube-local-prod-api:8000
https://greenkube.example.test/api/v1/...  -> greenkube-local-prod-api:8000
```

The application serves the frontend and `/api/v1` from the same API service. The route must preserve the `/api` prefix and the `Authorization` header. It must allow `GET`, `POST`, `PATCH`, and `DELETE`.

Example Gateway API `HTTPRoute` for the production-like instance:

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: greenkube-local-prod
  namespace: greenkube-local-prod
spec:
  parentRefs:
    - name: shared-gateway
      namespace: gateway-system
  hostnames:
    - greenkube.example.test
  rules:
    - matches:
        - path:
            type: PathPrefix
            value: /
      backendRefs:
        - name: greenkube-local-prod-api
          port: 8000
```

Because the frontend and API are same-origin, no CORS configuration is needed for this route. If the UI and API are exposed on different origins, configure `config.corsOrigins` and ensure the proxy handles `OPTIONS` requests and forwards the `Authorization` header.

## Checking either instance

```bash
kubectl -n <namespace> get pods
kubectl -n <namespace> get jobs
kubectl -n <namespace> logs deploy/<release> --tail=100
kubectl -n <namespace> logs deploy/<release>-api --tail=100
kubectl -n <namespace> logs deploy/<release>-controller --tail=100
kubectl -n <namespace> logs deploy/<release>-automation --tail=100
```

The development instance is convenient for demonstrations. The production-like instance should be used to validate authentication, separated components, leader election, migrations, and HTTPRoute behavior. Neither instance should be exposed publicly without authentication and network controls.

In the production profile, `production.leaderElection.enabled` configures the controller's namespaced Kubernetes Lease. The chart supplies `LEADER_ELECTION_ENABLED`, `LEADER_ELECTION_LEASE_NAME`, and `LEADER_ELECTION_LEASE_NAMESPACE`; only the Lease holder runs controller jobs, GitOps discovery, and sync observation. A controller that cannot read or update the Lease stays in standby. Leader election defaults to disabled outside this profile, preserving single-process and standalone operation.

## Local Gitea pull requests

The local validation repository is:

```text
http://127.0.0.1:18086/greenkube/greenkube-optimization-fixture
```

Create the Gitea port-forward when needed:

```bash
kubectl -n gitea port-forward svc/gitea-http 18086:3000
```

Configure a namespaced Kubernetes Secret and reference its key from `automation.providers`; do not copy provider credentials into Helm values, ConfigMaps, or shell output. The API and worker mount only the configured provider keys read-only, and the provider allowlist should include only the local test repository. The Recommendations page places pending and open pull requests in the **Pull requests** tab and removes those recommendations from **Active** until their pull request is closed.

In the production profile, the API ServiceAccount has read-only Kubernetes permissions for health and inventory endpoints. The controller and automation worker retain the same read-only discovery permissions plus the permissions required by their respective workflows. This keeps the Kubernetes service health check healthy when the API is accessed through a port-forward or an HTTPRoute.

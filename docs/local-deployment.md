# Local Minikube deployments

This runbook describes two supported local usage modes:

* **Development/demo**: real cluster data, no API key, intended for a local
  `kubectl port-forward`.
* **Production-like**: the production Helm profile, separate API/controller/
  automation deployments, and mandatory API-key authentication.

Both modes must use separate namespaces and releases. Do not reuse a
production Secret for a demo installation.

## Prerequisites

The Minikube Docker daemon must contain the image being deployed:

```bash
eval "$(minikube docker-env)"
docker build -t greenkube/greenkube:local-demo .
```

The examples below use the in-cluster Prometheus and OpenCost services. Adjust
the URLs if those services have different names in your cluster.

## Development/demo instance

Create a fresh namespace so no existing API key can be preserved by Helm:

```bash
helm upgrade --install greenkube-local-dev ./helm-chart \
  --namespace greenkube-local-dev \
  --create-namespace \
  --set profile=production \
  --set production.migration.enabled=false \
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

With no `GREENKUBE_API_KEY` in the namespace Secret and
`config.environment=development`, protected API routes are open. GreenKube
still collects real Prometheus/OpenCost/Kubernetes data.

Find the API service and forward it locally:

```bash
kubectl -n greenkube-local-dev get svc
kubectl -n greenkube-local-dev port-forward \
  svc/greenkube-local-dev-api 8000:8000
```

Open <http://localhost:8000>. The API and UI use the same origin, so no CORS
or browser token configuration is required.

## Production-like instance

Generate the API key outside the manifest and pass it to Helm:

```bash
export GREENKUBE_LOCAL_PROD_API_KEY="$(openssl rand -hex 32)"
```

Install the production-like release:

```bash
helm upgrade --install greenkube-local-prod ./helm-chart \
  --namespace greenkube-local-prod \
  --create-namespace \
  --set profile=production \
  --set production.migration.enabled=false \
  --set image.repository=greenkube/greenkube \
  --set image.tag=local-demo \
  --set config.environment=production \
  --set config.apiAuthMode=api_key \
  --set config.clusterName=minikube \
  --set config.defaultZone=FR \
  --set config.prometheus.url=http://prometheus-k8s.monitoring.svc.cluster.local:9090 \
  --set config.opencost.url=http://opencost.opencost.svc.cluster.local:9003 \
  --set-string secrets.apiKey="${GREENKUBE_LOCAL_PROD_API_KEY}" \
  --wait
```

Forward the production-like UI:

```bash
kubectl -n greenkube-local-prod port-forward \
  svc/greenkube-local-prod-api 8000:8000
```

Open <http://localhost:8000>. The UI displays an API authentication dialog.
Paste the value of `GREENKUBE_LOCAL_PROD_API_KEY` into that dialog. The
frontend keeps the key in memory only and sends it as a Bearer token.

The examples disable the separate migration hook because the PostgreSQL
StatefulSet is created by the same Helm transaction. The API startup migration
path applies the migrations after PostgreSQL becomes ready; verify the
`schema_migrations` table before using the instance.

For API clients:

```bash
curl \
  -H "Authorization: Bearer ${GREENKUBE_LOCAL_PROD_API_KEY}" \
  http://localhost:8000/api/v1/metrics/summary
```

Do not print the key or commit it to a values file. If the shell variable is
lost, retrieve the value from the release Secret using the normal Kubernetes
Secret access procedure.

## Ingress and HTTPRoute

Port-forwarding does not require an Ingress or HTTPRoute. For a shared local
or remote URL, route the UI and API through the same hostname:

```text
https://greenkube.example.test/            -> greenkube-*-api:8000
https://greenkube.example.test/api/v1/...  -> greenkube-*-api:8000
```

The application serves the frontend and `/api/v1` from the same API service.
The route must preserve the `/api` prefix and the `Authorization` header.
It must allow `GET`, `POST`, `PATCH`, and `DELETE`.

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

Because the frontend and API are same-origin, no CORS configuration is needed
for this route. If the UI and API are exposed on different origins, configure
`config.corsOrigins` and ensure the proxy handles `OPTIONS` requests and
forwards the `Authorization` header.

## Checking either instance

```bash
kubectl -n <namespace> get pods
kubectl -n <namespace> get jobs
kubectl -n <namespace> logs deploy/<release>-api --tail=100
kubectl -n <namespace> logs deploy/<release>-controller --tail=100
kubectl -n <namespace> logs deploy/<release>-automation --tail=100
```

The development instance is convenient for demonstrations. The production-like
instance should be used to validate authentication, separated components,
leader election, migrations, and HTTPRoute behavior. Neither instance should
be exposed publicly without authentication and network controls.

## Local Gitea pull requests

The local validation repository is:

```text
http://127.0.0.1:18086/greenkube/greenkube-optimization-fixture
```

Create the Gitea port-forward when needed:

```bash
kubectl -n gitea port-forward svc/gitea-http 18086:3000
```

The configured account is `greenkube`. GreenKube uses the Git token stored in
the production Secret; retrieve it without printing it:

```bash
kubectl -n greenkube-local-prod get secret greenkube-local-prod \
  -o jsonpath='{.data.GIT_TOKEN}' | base64 -d
```

Use that value as the password/token for local API or Git operations. The
Recommendations page places pending and open pull requests in the **Pull
requests** tab and removes those recommendations from **Active** until their
pull request is closed.

In the production profile, the API ServiceAccount has read-only Kubernetes
permissions for health and inventory endpoints. The controller and automation
worker retain the same read-only discovery permissions plus the permissions
required by their respective workflows. This keeps the Kubernetes service
health check healthy when the API is accessed through a port-forward or an
HTTPRoute.

# Security model

This page is GreenKube's threat model: what it protects, who can reach which endpoint, what each credential
grants, how the untrusted parts (repository content, discovered hosts, pushed signals) are contained, and which
risks remain. It is written for the operators who install GreenKube and for reviewers who assess it. Report
vulnerabilities as described in [SECURITY.md](../SECURITY.md).

## What GreenKube protects

| Asset | Why it matters |
|---|---|
| Git write access (provider tokens, the GitHub App private key) | GreenKube opens pull requests on your GitOps repositories. Git is its **only** write path: it never patches workloads in the cluster. A stolen Git credential is the highest-impact compromise. |
| The approval decision | An approved plan becomes a pull request. Who may approve, and what is approved automatically, must not be forgeable. |
| The savings ledger | Hash-chained records of realized savings; tampering must be detectable. |
| Cluster inventory | GreenKube reads workloads, pods, nodes and GitOps objects cluster-wide (read-only RBAC). |
| Accounts, sessions and API keys | They grant access to all of the above. |
| The database | Holds recommendations, plans, audit events, sessions (as SHA-256), API keys (as SHA-256) and the ledger. |

## Components and trust boundaries

| Component | Holds | Reachable from |
|---|---|---|
| **API** (`<release>-api`) | Database credentials; the session/API-key store; read-only projections of Git credentials (readiness only) | Users and machines, through `kubectl port-forward`, an Ingress or an HTTPRoute |
| **Controller** (`<release>-controller`) | Database credentials, Git provider credentials, the GitHub App key, webhook secrets, the scrape token; the Kubernetes service account (read-only cluster RBAC, a Lease, named Secrets) | Prometheus on its metrics port only (ClusterIP Service, no Ingress) |
| **Worker** (optional) | Same as the controller for plan execution | Prometheus on its metrics port only |
| **Renderer sandbox** | Nothing: no Secrets, no service-account token, no database | The controller only |
| **PostgreSQL** (bundled) | All persistent state | The api, controller and worker of the release only (`postgres.networkPolicy`, on by default) |
| **Credentials bootstrap hook** | A Role limited to `create` Secrets and `get`/`patch` on the release Secret | Runs once per install/upgrade |

## Authentication modes

| Mode | People | Machines | Main risk and control |
|---|---|---|---|
| `session` (default) | Local accounts, scrypt-hashed passwords, server-side sessions in an `HttpOnly; SameSite=Strict` cookie | Scoped API keys | Password guessing: lockout per username **and** client IP plus a per-IP budget; 12-character minimum |
| `oidc` | OpenID Connect, authorization code + PKCE, ID token verified against the JWKS | Scoped API keys | IdP misconfiguration: groups map to roles explicitly; `defaultRole: none` refuses unmapped users |
| `proxy` | Identity headers from an authentication proxy | Scoped API keys | Header spoofing: identity headers are accepted only from a TCP peer in `TRUSTED_PROXY_IPS` (required, enforced at startup and by the chart) |
| `api_key` | No accounts: the browser signs in with an API key, kept in an `HttpOnly; SameSite=Strict` cookie and re-checked on every request | The bootstrap key (its only administrator) and scoped keys | A single non-expiring admin credential: use only for headless installations |

Every browser mode ends in the same session: cookie-authenticated mutations need a same-origin `Origin`/`Referer`
and the session's `X-CSRF-Token`. HTTPS responses carry `Strict-Transport-Security`, and the chart makes the cookie
`Secure` whenever it exposes GreenKube over HTTPS. Details: [Authentication](authentication.md).

## Roles and scopes per route group

Scopes are checked twice: by a central table on every `/api/v1/*` and `/api/v2/*` request, and by a guard declared
on each mutating route, so a change to one never silently opens a route.

| Route group | Scope | Roles |
|---|---|---|
| Health, liveness, OpenAPI document, provider webhook ingress (`POST /api/v1/webhooks/{provider}`, verified by HMAC or token) | none | public |
| Every `GET` under `/api/v1` and `/api/v2` | `read` | viewer, operator, admin |
| Scans, ignore/restore, mark applied, plan previews, GitOps binding resolution | `write` | operator, admin |
| Plan approval (`POST /automation/plans/{id}/approve`), `POST /recommendations/{id}/apply-pr` | `approve` | operator, admin |
| Delivery report of `patch`/`ci` plans | `plans:deliver` | operator, admin |
| Push API (`POST /api/v1/signals`, as `push:<key name>` only) | `signals:write` | admin, or a key |
| KRR and Polaris report uploads | `signals:report` (`signals:write` accepted, deprecated) | admin, or a key |
| `/prometheus/metrics` and the controller/worker metrics listeners | `metrics:read`, or the scrape token | viewer and above, or a key |
| Users, roles, API keys, the repository allowlist | `admin` | admin |
| `/auth/me`, own sessions, password change, sign-out | authenticated | any |

## Credentials

| Credential | Where it lives | Who reads it | Grants |
|---|---|---|---|
| Git provider token | Secret `greenkube-git-credentials` (or `automation.providers[].secretName`) | Controller/worker (mounted read-only); API reads only which keys exist | Pull requests on allowlisted repositories, bound to one Git host (`host` key) |
| GitHub App private key | Same Secret, keys `appId` + `privateKey` | Controller/worker | Short-lived installation tokens, limited to the repositories the App is installed on |
| Webhook secrets | `automation.providers[].webhookSecretKey` | API (verification only) | Nothing: they authenticate provider deliveries |
| Database password / DSN | Secret `<release>` (`DB_CONNECTION_STRING`) | api, controller, worker | The whole database |
| Scoped API keys `gk_<id>_<secret>` | Database, as SHA-256 | Whoever holds the key | Their scopes, until revoked or expired |
| Scrape token `GREENKUBE_METRICS_TOKEN` | Secret `<release>`, generated once | api, controller, worker; the ServiceMonitors | `metrics:read` only |
| Bootstrap key `GREENKUBE_API_KEY` (deprecated) | Secret `<release>`, generated only for `api_key` mode | api, worker | `auth.legacyApiKey`: `admin` (default, for upgraded installs), `metrics` or `disabled` |
| Session tokens | Browser cookie; database as SHA-256 | The browser | The user's role, until sign-out, idle timeout or absolute lifetime |
| Initial admin password | Secret `<release>-initial-admin` | The operator, once | Erased after the first password change |

Generated credentials are created by the bootstrap hook outside the Helm release, so `helm template` (Argo CD) never
rotates them. Credentials are never written to ConfigMaps, logs, API responses, plan bodies or the browser.

## Untrusted inputs

| Input | Threat | Control |
|---|---|---|
| Repository content (Helm charts, Kustomize overlays) | Template code that reads files, the network or the cluster | Rendered in the **renderer sandbox**: a separate Pod without Secrets, service-account token or database, read-only root filesystem, dropped capabilities, bounded CPU/memory/PIDs and an egress-deny NetworkPolicy checked by a canary (`renderer_egress_unenforced` when the CNI does not enforce it). Archives are bounded (100 MiB compressed, 500 MiB extracted, 20,000 files), with no links or paths outside the snapshot. |
| Remote Helm charts | A malicious or swapped chart | Only the exact version the cluster deployed, verified against the repository digest, rendered offline (twice: nondeterministic output fails closed); never loopback, link-local or metadata hosts; `automation.helm.remoteCharts.allowedHosts` narrows the hosts, `enabled=false` disables them. |
| Hosts named by GitOps sources | Anyone who can create an Argo CD Application or a Flux source names the host | Git credentials are sent only to the host the credential is bound to; the provider probe is an unauthenticated `GET` over **HTTPS** only, never to loopback, link-local, multicast or unspecified addresses, and follows no redirect. |
| Pushed signals and reports | A machine key inflating its own evidence | A push key writes only `push:<key name>`; reports need `signals:report`; pushed signals alone never produce a deletion, never stand for another engine family, and **all pushed signals together count as at most one independent consensus vote**, so they cannot satisfy the two-independent-sources condition of auto-approval by themselves. |
| Provider webhooks | Forged deliveries | Provider-native HMAC/token verification, deduplication, and a re-fetch of the provider state before any lifecycle change. |
| `X-Forwarded-*` and identity headers | Spoofed client address, scheme or identity | Read only from a TCP peer in `TRUSTED_PROXY_IPS`; the client address is the rightmost `X-Forwarded-For` hop that is not a trusted proxy. |

## Outbound requests

| From | To | Credentials sent |
|---|---|---|
| Controller | Prometheus, OpenCost (in-cluster or configured URLs) | Optional basic/bearer credentials you configure |
| Controller | Electricity Maps, Wattnet, Boavizta (when configured) | Their API tokens |
| Controller/worker | Git providers (GitHub, GitLab, Gitea) | The Git credential, to its bound host only |
| Controller | Remote Helm repositories and OCI registries | None (anonymous; registry bearer tokens never follow a redirect to another host) |
| Controller | Self-hosted provider probe (`/api/v4/version`, `/api/v1/version`) | None |
| Connectors (VPA, Goldilocks, Kubecost, GKE, Azure Advisor...) | Their in-cluster or cloud endpoints | Their own configured credentials |
| Renderer sandbox | Nothing (egress denied) | None |

Restrict controller egress with `monitoring.networkPolicy.additionalEgress` (opt-in: the right egress list depends on
where Prometheus, OpenCost and your Git hosts live).

## Network policies

- `postgres.networkPolicy` (on by default): only the release's api, controller and worker, and pods labelled
  `greenkube.cloud/postgres-client: "true"` (a backup or copy Job), reach PostgreSQL. The renderer sandbox, the
  KRR/Polaris jobs and other namespaces cannot.
- The renderer sandbox always has an egress-deny policy.
- `monitoring.networkPolicy` (opt-in): ingress to the API and metrics ports from Prometheus (plus the KRR/Polaris
  jobs and `additionalIngress`, e.g. the Ingress controller) and egress limited to DNS, PostgreSQL, the renderer and
  `additionalEgress`. It is opt-in because a default egress list cannot know where Prometheus, OpenCost, the Kubernetes
  API and your Git hosts are; an API ingress policy that allows everything on the API port would add nothing.

NetworkPolicy needs a CNI that enforces it (Calico, Cilium, the cloud CNIs with policy enabled). Without one these
objects are accepted and ignored.

Kubernetes API egress on Cilium: an `additionalEgress` `ipBlock` for the API Service (`10.96.0.1/32:443`) or for the
API server endpoints (`kubectl get endpointslice -n default -l kubernetes.io/service-name=kubernetes`, port 6443)
does not match. Cilium (with kube-proxy replacement, it sees the translated destination) treats the control-plane
node as the `kube-apiserver` entity, which `ipBlock` never selects by default; the controller then cannot read its
Lease and does no work. Allow it with a CiliumNetworkPolicy next to the chart's policies, or set Cilium's
`policyCIDRMatchMode: nodes` and keep the `ipBlock` on the endpoints:

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: greenkube-kube-apiserver
  namespace: greenkube
spec:
  endpointSelector:
    matchExpressions:
      - {key: app.kubernetes.io/instance, operator: In, values: [greenkube]}
      - {key: app.kubernetes.io/component, operator: In, values: [api, controller, worker]}
  egress:
    - toEntities: [kube-apiserver]
      toPorts:
        - ports: [{port: "6443", protocol: TCP}, {port: "443", protocol: TCP}]
```

## Hardening checklist for production

1. Serve GreenKube over HTTPS (an Ingress with `tls` or an https HTTPRoute) and set `config.trustedProxyIps` to the
   Ingress controller or gateway addresses (often the pod CIDR). `helm install` warns when it is missing.
2. Prefer `oidc` or `proxy` mode with `auth.roles` mapped to IdP groups; keep `auth.localLogin` only as break-glass.
3. Upgraded installs: migrate scripts from the bootstrap key to scoped keys, then set `auth.legacyApiKey: disabled`.
4. Give each machine its own scoped key with the narrowest scopes and an expiry (`metrics:read` for Prometheus,
   `signals:report` for KRR/Polaris, `read` + `plans:deliver` for CI delivery).
5. Use a CNI that enforces NetworkPolicy, so the renderer and PostgreSQL policies take effect.
6. Keep auto-approval off, or limit it to `low` risk (`automation.autoApprove`).
7. Use a GitHub App (repository-scoped, short-lived tokens) rather than a personal access token.

## Residual risks

- **Cluster administrators** and anyone who can read Secrets or exec into GreenKube Pods in its namespace can read
  every credential. GreenKube cannot defend against them; RBAC on the namespace is your boundary.
- **Distributed password guessing.** The sign-in lockout is per username and client address, so guessing spread over
  many addresses is bounded per address only. Use SSO or keep the sign-in page off the public internet.
- **Shared client address.** Without `config.trustedProxyIps`, every client behind an Ingress shares one address:
  one client can exhaust the API rate limit and the per-IP sign-in budget for a window.
- **Per-replica limits.** Rate limits and lockouts are kept in each API process; with N replicas the effective budget
  is N times larger.
- **The bootstrap key stays an admin key on upgraded installs** until `auth.legacyApiKey` is changed.
- **A compromised push or report key** can still shape recommendations (one vote, never a deletion, never an
  auto-approved decrease on its own). Revoke it in **Settings > API keys**.
- **Unenforced NetworkPolicy.** On a CNI that ignores NetworkPolicy, the renderer sandbox can reach the network
  (`renderer_egress_unenforced` is reported) and PostgreSQL is reachable from any pod that knows its password.
- **Controller egress** is unrestricted by default: the controller can reach any host a GitOps source names with an
  unauthenticated `GET` (the HTTPS provider probe, remote chart fetches within the documented host rules).
- **Metrics without a scrape token.** With `secrets.existingSecret` and no `GREENKUBE_METRICS_TOKEN`, the controller
  metrics listener serves its operational counters without authentication on the pod network.

## Continuous checks

CI runs CodeQL (`security-extended`, Python and JavaScript), Trivy on the images, the Dockerfile and the Helm chart,
`npm audit`, and Dependabot updates. Helm unit tests assert the Pod Security `restricted` profile, the RBAC and the
NetworkPolicies described here.

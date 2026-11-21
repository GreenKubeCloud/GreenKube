# Authentication

GreenKube has two kinds of callers:

- **People** sign in to the dashboard with a username and password. The API issues a server-side session held in an
  `HttpOnly` cookie, so a reload, a new tab or (with **Keep me signed in**) a browser restart keeps you signed in.
- **Machines** (CI pipelines, Prometheus, scripts, the push API) send a scoped API key as
  `Authorization: Bearer gk_<id>_<secret>`.

Both resolve to one identity, the *principal*, which GreenKube records in audit events, plan approvals and ledger
entries: `user:<username>` for people, `apikey:<key name>` for scoped keys, and `api-key:<digest>` for the deprecated
bootstrap key. Prometheus scrapes with a dedicated `metrics:read` [scrape token](#prometheus).

The threat model behind these choices (who can reach what, which credential grants what, residual risks) is in
[Security model](security-model.md).

The mode is `config.apiAuthMode` (setting `API_AUTH_MODE`):

| Mode | Browser sign-in | API keys | Use |
|---|---|---|---|
| `session` (default) | local accounts (username and password) | scoped keys (and a bootstrap key kept from an earlier install) | Every installation |
| `proxy` | single sign-on through an authentication proxy (oauth2-proxy, Authentik, Pomerium), plus local accounts | same | Clusters that already front their tools with an auth proxy |
| `oidc` | native OpenID Connect (**Sign in with ...**), plus local accounts | same | Any OpenID Connect provider (Keycloak, Dex, Entra ID, Okta, Google, GitLab) |
| `api_key` | an API key, no accounts (`POST /auth/login` answers `409`, see [Browser sign-in with an API key](#browser-sign-in-with-an-api-key)) | scoped keys and the bootstrap key | Headless installations driven only by machines |

Every browser mode ends in the same server-side session, cookie and CSRF protection described below; only the way
the user proves who they are differs. See [Single sign-on](#single-sign-on) and [Roles](#roles).

### Browser sign-in with an API key

In `api_key` mode there are no accounts: the sign-in page asks for an API key (the bootstrap key
`GREENKUBE_API_KEY` or a scoped `gk_` key with the `read` scope; the scrape token and `metrics:read`-only keys are
refused with `403`). `POST /api/v1/auth/api-key-login` (`{"key": "...", "remember": false}`) checks the key like a
Bearer key and answers with a signed cookie, never the key itself:

- **Format:** `v1.<kid>.<exp>.<mac>`, where `kid` is the scoped key id or `legacy` for the bootstrap key, `exp` the
  expiry (Unix time) and `mac` an HMAC-SHA256 over the rest. The signing key is derived from the bootstrap key, so
  this sign-in needs `GREENKUBE_API_KEY` (without it the endpoint answers `409`), and **rotating the bootstrap key
  signs every browser out**.
- **Lifetime:** 12 hours, or `auth.session.rememberLifetime` (30 days by default) with "Keep me signed in".
- **Every request** checks the signature (constant time) and the expiry, re-reads a scoped key and requires it
  active, so revoking or expiring the key signs its browsers out. A cookie that fails a check is refused and cleared.
- **Cookie flags:** `HttpOnly; SameSite=Strict`, and `Secure` over HTTPS. Cookie-authenticated mutations need a
  same-origin `Origin` and the `X-CSRF-Token` returned by the sign-in and `/auth/me`, which differs for every cookie.
- **HTTPS required:** over plain HTTP the sign-in answers `400 HTTPS_REQUIRED`, except for a loopback host
  (`kubectl port-forward` to `localhost`). HTTPS is recognised directly, through `X-Forwarded-Proto` from
  `config.trustedProxyIps`, or when `auth.session.secureCookie` is `always`, which the chart renders with
  `ingress.tls` or an https HTTPRoute. If TLS ends at an ingress or load balancer the chart does not render, set
  `config.trustedProxyIps` to its addresses or `auth.session.secureCookie: always`.
- **Rate limit:** failed attempts count against `auth.loginRateLimit` per client IP and attempted key, plus the
  per-IP budget, so one client's typos do not lock out the others behind the same address.
- **Sign out** clears the cookie in the browser but does not revoke it on the server: a copy of the cookie stays
  valid until its `exp`, unless the key is revoked or the bootstrap key rotated.

For user accounts, roles and per-person audit, use `session` mode instead.

## First sign-in

On the first start without any account, the API creates the account `admin` (`auth.admin.username`) with an
initial password. `helm install` prints the command that reads it:

```bash
kubectl -n greenkube get secret greenkube-initial-admin \
  -o jsonpath='{.data.password}' | base64 -d; echo
```

1. Open the dashboard and sign in as `admin` with that password.
2. GreenKube asks you to choose a new password (at least 12 characters) before anything else.
3. GreenKube then erases the initial password from the Secret. The Secret stays, empty, so a later
   `helm upgrade` or Argo CD sync never issues a second initial password. From then on, the notes printed by
   `helm upgrade` no longer point to that Secret: they print the reset command of [Recovery](#recovery) instead.

Where the initial password comes from:

| Configuration | Initial password | Forced change |
|---|---|---|
| Default | Generated once by the `<release>-credentials-bootstrap` hook into the Secret `<release>-initial-admin` | Yes |
| `auth.admin.existingSecret` | The key `auth.admin.existingSecretKey` (default `password`) of your Secret, mounted into the API as `AUTH_ADMIN_PASSWORD` | No: you chose it |
| `rbac.create: false` | Nothing can be generated: set `auth.admin.existingSecret` | |
| Local development (`greenkube-api` outside a cluster) | Set `AUTH_ADMIN_PASSWORD` before the first start | No |

The password only seeds the account the first time: changing the Secret afterwards does not change the account.

A development instance (`ENVIRONMENT=development`) without any account and without `GREENKUBE_API_KEY` stays open,
as before; the sidebar shows **Sign-in disabled (development)**. Production never serves anonymous requests.

### Why the hook creates the Secret

The roadmap asked the API to create `<release>-initial-admin` itself, with a Role limited to that one name.
Kubernetes RBAC cannot restrict `create` to a resource name: such a Role would have to let the API create *any*
Secret in its namespace, including service-account token Secrets that the token controller fills in for any
ServiceAccount of the namespace. GreenKube therefore lets the existing credentials bootstrap hook (which already
creates Secrets, runs before every install, upgrade and Argo CD sync, and is deleted when it succeeds) create the
Secret once, and gives the API a Role limited to `get` and `patch` on `<release>-initial-admin` only. The API
never writes any other Secret.

## Sessions

| Setting | Helm value | Default |
|---|---|---|
| `SESSION_COOKIE_NAME` | | `greenkube_session` |
| `SESSION_COOKIE_SECURE` | `auth.session.secureCookie` | `auto` (`always` with an HTTPS Ingress or HTTPRoute) |
| `HSTS_MAX_AGE` | `auth.session.hsts.maxAge` | `31536000` (`0` disables `Strict-Transport-Security`) |
| `HSTS_INCLUDE_SUBDOMAINS` | `auth.session.hsts.includeSubdomains` | `false` |
| `AUTH_SESSION_IDLE_TIMEOUT` | `auth.session.idleTimeout` | `12h` (sliding) |
| `AUTH_SESSION_ABSOLUTE_LIFETIME` | `auth.session.absoluteLifetime` | `7d` |
| `AUTH_SESSION_REMEMBER_LIFETIME` | `auth.session.rememberLifetime` | `30d` (with **Keep me signed in**) |
| `AUTH_LOGIN_RATE_LIMIT` | `auth.login.rateLimit` | `5/minute` |
| `TRUSTED_PROXY_IPS` | `config.trustedProxyIps` | empty |

- The cookie holds an opaque 256-bit token. The database stores only its SHA-256, in the table `auth_sessions`,
  which every API replica shares (PostgreSQL).
- The cookie is `HttpOnly`, `SameSite=Strict` and `Path=/` (or `ROOT_PATH`). Without **Keep me signed in** it is a
  browser-session cookie; with it, it lasts the remember lifetime.
- `Secure` follows `SESSION_COOKIE_SECURE`: `auto` sets it when the browser uses HTTPS, either directly or through a
  proxy listed in `TRUSTED_PROXY_IPS` that sends `X-Forwarded-Proto: https`; `always` sets it unconditionally;
  `never` is for plain-HTTP tests only. The chart renders `always` for `auto` when it exposes GreenKube over HTTPS
  (`ingress.enabled` with an `ingress.tls` entry, or `httpRoute.enabled` with `httpRoute.scheme: https`), so the
  cookie is `Secure` behind a TLS Ingress even before `config.trustedProxyIps` is set.
- HTTPS responses carry `Strict-Transport-Security: max-age=31536000` (`HSTS_MAX_AGE`; `includeSubDomains` with
  `HSTS_INCLUDE_SUBDOMAINS`). A response is HTTPS when the request is, directly or per `X-Forwarded-Proto` from
  `TRUSTED_PROXY_IPS`, and always when `SESSION_COOKIE_SECURE=always` (browsers ignore the header over plain HTTP,
  so a `kubectl port-forward` is not affected).
- Signing out revokes the session on the server. Changing a password revokes every session of the account and
  issues a new one for the current browser.
- **Account & sessions** (sidebar, or `/settings/account`) lists your active sessions and signs out any of them.
- Expired sessions, and sessions revoked more than a day ago, are purged daily.

Serve GreenKube over HTTPS for remote access (an Ingress with TLS): the dashboard shows a warning when it is reached
over plain HTTP from anything other than `localhost`. Behind an Ingress or a Gateway, set `config.trustedProxyIps`
to the addresses the API sees the Ingress controller (or gateway) connect from: usually the pod CIDR of the cluster
(for example `10.244.0.0/16` on kind and Minikube, the VPC pod range on EKS) or the controller pods' addresses. Only
then does GreenKube read the client address from `X-Forwarded-For` (the rightmost hop that is not a trusted proxy)
and `X-Forwarded-Proto`. Without it, every request appears to come from the Ingress controller, so API rate limits
and sign-in lockouts are shared by all users; `helm install` prints a warning when an Ingress or HTTPRoute is enabled
with an empty `config.trustedProxyIps`.

### Cross-site request protection

- Every mutation (`POST`, `PUT`, `PATCH`, `DELETE`) authenticated by a session cookie needs:
  - an `Origin` (or `Referer`) header naming the GreenKube host itself, or an origin listed explicitly in
    `config.corsOrigins`; and
  - an `X-CSRF-Token` header equal to the session's CSRF token, which the dashboard reads from `GET /auth/me`.
- A form or script on another website therefore cannot act as the signed-in user, and the `SameSite=Strict` cookie is
  not even sent with cross-site requests.
- The API stays same-origin by default. A wildcard `config.corsOrigins: "*"` never allows credentials and never
  passes the origin check.
- Requests authenticated with an `Authorization` header are not exposed to cross-site forgery and need no CSRF token.

### Brute-force protection

Failed sign-ins are counted per username **and client IP** (`AUTH_LOGIN_RATE_LIMIT`) and per client IP across
usernames (four times that budget). Past its budget, the username and IP pair is locked for one rate-limit window,
doubling at every further exhaustion up to 15 minutes; a successful sign-in resets it. The per-IP budget is a plain
moving window without escalation: it bounds password spraying from one address. Locked attempts answer `429` with
`Retry-After`. Errors never say whether a username
exists, and an unknown username costs the same password-hashing time as a known one. A wrong current password on
`POST /api/v1/auth/password` counts as a failed sign-in for that user, so a stolen session cookie does not allow
unthrottled guessing either.

The lockout never covers a username alone: an attacker guessing the `admin` password from one address locks only
that address out of that account, and the real administrator still signs in from theirs. The trade-off is that
guessing distributed over many addresses is bounded per address only, which is why passwords need at least 12
characters and why the sign-in page is best kept off the public internet. All of this needs the real client
address: behind an Ingress or load balancer, set `config.trustedProxyIps` (see [Sessions](#sessions)); otherwise
every user shares the proxy's address, and one client exhausting the per-IP budget blocks everyone for a window.

#### Failed API keys

Wrong API keys are throttled too, so that an operator-chosen bootstrap key cannot be guessed online at the global
`API_RATE_LIMIT`. Every failed `Authorization: Bearer` authentication (an unknown, revoked or expired key, the wrong
bootstrap key or scrape token, or a non-Bearer scheme) and every rejected API-key browser cookie (`api_key` mode)
counts against a per-client-IP budget of four times `AUTH_LOGIN_RATE_LIMIT` (20 a minute by default), in a moving
window with no exponential lockout. Past it, requests with a credential from that address answer `429
TOO_MANY_ATTEMPTS` with `Retry-After` until the window frees up.

- **The budget is per address, not per client.** While an address is throttled, every credential sent from it waits,
  the correct one included: checking it first would tell an attacker which guess was right at the full rate. Clients
  that share an address (behind a NAT, or behind an Ingress without `config.trustedProxyIps`) share the budget, so one
  misconfigured client can block the others for at most one window. A correct key from any other address is never
  affected. The client address is resolved by the same trusted-proxy logic as sign-ins.
- **Successful authentications never consume or reset it**, so a valid low-privilege key does not buy more guesses
  at another one.
- **Prometheus scrapes have their own bucket.** Failures on `/prometheus/metrics` and failures on the API routes are
  counted apart, so a client sending a wrong key to the API never stops a correct scrape with the scrape token or a
  `metrics:read` key, even from the same address. The controller and worker metrics listeners are not throttled.
- An open development instance (no credential configured) has nothing to guess and is never throttled.

At most two password hashes run at once per API process (each scrypt run uses about 32 MiB), so a burst of sign-in
requests queues instead of exhausting the Pod's memory.

Counters live in each API process: with several replicas, an attacker gets at most the budget times the number of
replicas. `greenkube_auth_failures_total{reason}` counts `bad_credentials`, `rate_limited`, `invalid_session`,
`invalid_api_key`, `csrf`, `forbidden_scope`, `untrusted_proxy_header` and `sso` rejections.

### Passwords

Passwords are hashed with scrypt from the Python standard library (`N=2^15`, `r=8`, `p=1`, 16-byte random salt,
32-byte key), stored as `scrypt$N$r$p$salt$hash`. A hash with weaker parameters is upgraded at the next successful
sign-in. Passwords need at least 12 characters (at most 256) and must differ from the username.

## Roles

| Role | Can |
|---|---|
| `viewer` | Read everything (dashboards, recommendations, plans, changes, ledger, settings) |
| `operator` | Everything a viewer can, plus approve plans (which opens pull requests), cancel and delete plans, preview rollbacks, ignore and restore recommendations, mark them applied, request re-measurements, report out-of-band deliveries |
| `admin` | Everything, plus users and roles, API keys and the repository allowlist |

- Roles are enforced on the server, twice: the global authentication dependency maps every request to a scope, and
  each mutating route also declares the scope it needs (`require_scope`), so a gap in one table never opens a route.
  A viewer gets `403` (`INSUFFICIENT_SCOPE`) on plan approval and every other mutation.
- The bootstrap account is an `admin`. Administrators manage local accounts in **Account > Users and roles**
  (`/settings/account`) or with `GET/POST /api/v1/auth/users` and `PATCH /api/v1/auth/users/{id}` (`{"role": ...}`,
  `{"disabled": true}`). A new local account chooses its own password at its first sign-in. Disabling an account
  signs out all of its sessions. The last active admin can be neither demoted nor disabled (`409 LAST_ADMIN`).
- Single sign-on accounts get their role from their groups (below) at every sign-in; it cannot be edited in the UI.
- Operator (`write`, `approve`, `plans:deliver`) covers the day-to-day loop: plans and their delivery,
  recommendation decisions, GitOps binding resolution and GitOps discovery and provider
  routes (`/api/v1/gitops/*`). Admin is required for users and roles (`/api/v1/auth/users`), API keys
  (`/api/v1/auth/api-keys`) and the repository allowlist.

## Single sign-on

Both SSO modes create, at the first sign-in, a GreenKube account bound to the identity provider (`auth_source`
`proxy` or `oidc`) and then issue the usual server-side session. These accounts have no password. An SSO identity
never takes over an existing account: when its username is already used (for example by the local `admin`), the
sign-in is refused with `ACCOUNT_CONFLICT`.

Local accounts keep signing in with their password in both modes (`auth.localLogin: true`, the default), so the
bootstrap `admin` remains a break-glass account when the identity provider is down. Set `auth.localLogin: false` to
offer single sign-on only.

### Group to role mapping

```yaml
auth:
  roles:
    groups:
      admin: [platform-admins]
      operator: [sre, "acme:oncall"]
    defaultRole: viewer   # or none: refuse users in no listed group
```

A user receives the highest role any of their groups grants, else `defaultRole`. With `defaultRole: none`, a user in
no listed group is refused (`NO_ROLE`). The role is recomputed at every sign-in (and on every request in `proxy`
mode), so removing someone from a group takes effect at their next session (immediately behind a proxy).

### Authentication proxy (`config.apiAuthMode: proxy`)

```yaml
config:
  apiAuthMode: proxy
  trustedProxyIps: 10.42.0.0/16      # the proxy Pods or the Ingress controller, required
auth:
  proxy:
    userHeaders: X-Forwarded-User      # defaults (oauth2-proxy --pass-user-headers)
    groupsHeaders: X-Forwarded-Groups
```

- GreenKube accepts identity headers **only** when the TCP connection comes from an address listed in
  `config.trustedProxyIps`. The check uses the connection itself, never the `X-Forwarded-For` client the proxy
  reports, so proxied requests are recognised and an end user whose own address falls in a trusted range does not
  pass for the proxy. A request carrying an identity or groups header from any other peer is rejected with
  `401 UNTRUSTED_PROXY_HEADER` (and counted as `greenkube_auth_failures_total{reason="untrusted_proxy_header"}`),
  whatever cookie or account it also carries. The chart refuses to render `proxy` mode without `trustedProxyIps`,
  and the API refuses to start without it.
- **Configure only the headers your proxy sets and overwrites.** A proxy strips or overwrites the header family it
  manages and passes any other header through unchanged, so a client could add a second family (for example
  `X-Auth-Request-User: alice` and `X-Auth-Request-Groups: platform-admins` next to oauth2-proxy's
  `X-Forwarded-*`). GreenKube never merges families: the n-th `groupsHeaders` entry belongs to the n-th
  `userHeaders` entry, only the groups header paired with the user header is read, and a request carrying headers
  of more than one configured family, or a repeated user header, is refused with `401 INVALID_IDENTITY`. List
  several families only when the proxy overwrites all of them.
- List only the proxy's own addresses: every peer in that list can claim any identity. Keep the GreenKube Service
  unreachable around the proxy, for example with `monitoring.networkPolicy`. A session started through the proxy is
  only accepted through it: replayed from any other peer it is refused with `401 UNTRUSTED_PROXY_HEADER`.
- The first `GET /api/v1/auth/me` of an identity starts its session (the dashboard does this on load). Reads work at
  once; mutations need the session's CSRF token, so a header-only request is refused with `CSRF_REJECTED` and the
  dashboard refreshes `/auth/me` and retries.
- oauth2-proxy as a reverse proxy: `--pass-user-headers` (on by default) sets `X-Forwarded-User` and
  `X-Forwarded-Groups`, the defaults. oauth2-proxy behind ingress-nginx `auth-url`: `--set-xauthrequest` returns
  `X-Auth-Request-User` and `X-Auth-Request-Groups` to the Ingress, which copies them with
  `nginx.ingress.kubernetes.io/auth-response-headers: X-Auth-Request-User,X-Auth-Request-Groups`; set
  `auth.proxy.userHeaders: X-Auth-Request-User` and `auth.proxy.groupsHeaders: X-Auth-Request-Groups`, and list the
  ingress controller Pods in `trustedProxyIps`. Authentik's proxy outpost sends `X-authentik-username` and
  `X-authentik-groups`: set `auth.proxy.userHeaders: X-authentik-username` and
  `auth.proxy.groupsHeaders: X-authentik-groups`. Pomerium: forward the claims as headers (`jwt_claims_headers`)
  and point the two settings at them.
- A local account that signs in with its password keeps its own session even through the proxy.

### OpenID Connect (`config.apiAuthMode: oidc`)

```yaml
config:
  apiAuthMode: oidc
  externalUrl: https://greenkube.example.com
auth:
  oidc:
    issuer: https://keycloak.example.com/realms/acme
    clientId: greenkube
    existingSecret: greenkube-oidc        # key clientSecret; omit for a public client
    groupsClaim: groups
    providerName: Acme SSO
  roles:
    groups:
      admin: [greenkube-admins]
      operator: [greenkube-operators]
```

Register `https://greenkube.example.com/api/v1/auth/oidc/callback` as the redirect URI (or set
`auth.oidc.redirectUrl`). The sign-in page then shows **Sign in with Acme SSO**.

How the flow is protected:

- **Authorization code with PKCE** (`S256`), never the implicit flow. The code verifier, the `state` and the `nonce`
  are 256-bit random values stored server-side (table `auth_oidc_logins`, keyed by the SHA-256 of the state), so any
  API replica can finish the sign-in. A pending sign-in expires after ten minutes and is deleted when used: a
  replayed callback fails (`state_expired`).
- The `state` is also bound to the browser by an `HttpOnly`, `SameSite=Lax` cookie limited to
  `/api/v1/auth/oidc/`: a callback opened in another browser fails (`state_mismatch`), which defeats login CSRF.
- **Discovery** must name the configured issuer exactly, endpoints must use `https` (plain `http` only outside
  production) and, when the provider lists its PKCE methods, `S256` must be one of them.
- **ID token**: the signature is verified against the provider's JWKS (`RS256/384/512`, `PS256/384/512`,
  `ES256/384/512`, `EdDSA`; `none` and shared-secret `HS*` algorithms are refused, keys are re-fetched once when an
  unknown `kid` appears). Then `iss`, `aud` (and `azp` with several audiences), `exp`, `iat`, `nbf` (60-second
  leeway), `nonce` and `sub` are checked.
- The account is keyed by the issuer's `sub`, never by the username, so renaming a user at the provider keeps the
  same GreenKube account. The username comes from `auth.oidc.usernameClaim` (then `preferred_username`, `email`,
  `sub`), the groups from `auth.oidc.groupsClaim`. Some providers only include groups when asked: add their scope
  to `auth.oidc.scopes` (for example `groups` for Dex or Okta) or configure a groups mapper (Keycloak).
- A confidential client authenticates with `client_secret_basic` (or `client_secret_post` when the provider supports
  only that). Without a secret GreenKube is a public client, protected by PKCE.
- Failures go back to the sign-in page with a stable code (`?sso_error=denied`, `state_mismatch`, `state_expired`,
  `invalid_token`, `provider_unavailable`, `token_exchange_failed`, `no_role`, `account_conflict`,
  `account_disabled`) shown as a message; the details go to the API log and to
  `greenkube_auth_failures_total{reason="sso"}`. The return path after sign-in is always a local path.
- GreenKube sessions are independent of the provider's: signing out of GreenKube does not sign out of the provider,
  and the GreenKube session lasts `auth.session.*`.

## API keys

Administrators create keys in **Settings > API keys** (`/settings/api-keys`) or with the API:

```bash
curl -X POST https://greenkube.example.com/api/v1/auth/api-keys \
  -H "Authorization: Bearer ${ADMIN_KEY}" -H 'Content-Type: application/json' \
  -d '{"name": "prometheus", "scopes": ["metrics:read"], "expires_in_days": 365}'
```

- The response contains the full key, `gk_<8-character id>_<secret>`, **once**. GreenKube stores only the SHA-256 of
  the secret and shows the `gk_<id>` prefix in listings.
- Revoking a key (`DELETE /api/v1/auth/api-keys/{id}`) takes effect immediately. A key can also expire.
- `last_used_at` is updated at most once a minute.

| Scope | Grants |
|---|---|
| `read` | Every `GET` under `/api/v1` and `/api/v2` (changes and measurements) |
| `write` | Mutations: scans, ignore/restore, mark applied, plan previews, binding resolution |
| `approve` | Plan approval, which opens pull requests: `POST /api/v1/automation/plans/{id}/approve` |
| `plans:deliver` | Report that an approved plan was delivered outside GreenKube (`POST /api/v1/automation/plans/{id}/delivery`, `patch` and `ci` delivery modes). A CI job needs `read` + `plans:deliver` only |
| `signals:write` | Pushing signals: `POST /api/v1/signals` ([Push API](sources/push.md)). A key pushes batches only as `push:<key name>` (`403 push_client_mismatch` otherwise) |
| `signals:report` | Uploading reports: `POST /api/v1/sources/krr/reports` ([KRR](sources/krr.md)) and `POST /api/v1/sources/polaris/reports` ([Polaris](sources/polaris.md)). A `signals:write` key is still accepted there for one minor release, with a `Deprecation: true` response header and a warning in the API log |
| `metrics:read` | Only the Prometheus endpoint `/prometheus/metrics` |
| `admin` | Everything, including API keys |

User roles map to the same scopes: `viewer` (`read`, `metrics:read`), `operator` (adds `write`, `approve` and `plans:deliver`) and
`admin` (everything). See [Roles](#roles).

Pushed signals never corroborate each other: every signal that arrives through the push API (`push:<client>`
batches, KRR and Polaris reports) counts as at most **one** independent consensus vote together, whatever the key and
the client name. One `signals:write` key could otherwise push as two clients, or as a client plus a report, and meet
the "two independent sources" condition that lets [auto-approval](automation.md) approve a request decrease.

### Prometheus

Prometheus scrapes three endpoints: the API's `/prometheus/metrics`, the controller's metrics listener and, with a
dedicated worker, the worker's. All three accept the **scrape token** `GREENKUBE_METRICS_TOKEN`: a random token the
credentials bootstrap hook generates once into the Secret `<release>` (or `secrets.metricsToken`), which grants
`metrics:read` and nothing else (`403` on `/api/v1/recommendations`). The chart's ServiceMonitors send it as their
bearer token; the controller never receives the admin API key.

With `secrets.existingSecret`, add a `GREENKUBE_METRICS_TOKEN` key to your Secret and set
`secrets.existingSecretHasMetricsToken: true`; until then the ServiceMonitors keep scraping with its
`GREENKUBE_API_KEY`, as before. Without `rbac.create`, supply `secrets.metricsToken`.

The ServiceMonitors default to the release namespace, next to the scrape token, because Prometheus Operator reads a
ServiceMonitor's bearer token Secret in the ServiceMonitor's own namespace. With
`monitoring.serviceMonitor.namespace` set elsewhere, the chart copies only the scrape token there, as Secret
`<release>-scrape-token` ([Monitoring](configuration.md#monitoring-monitoring)).

You can also scrape the API with a scoped key of your own. Create a key with only the `metrics:read` scope, store it
in a Secret in the namespace of the ServiceMonitor, and reference it:

```bash
kubectl -n monitoring create secret generic greenkube-metrics --from-literal=token='gk_...'
```

```yaml
monitoring:
  serviceMonitor:
    enabled: true
    bearerTokenSecret:
      name: greenkube-metrics
      key: token
```

That key is rejected everywhere else (`403` on `/api/v1/recommendations`, for example). The controller and worker
listeners do not read the account store: they accept the scrape token (and the bootstrap key while it is enabled).

### The bootstrap API key

`GREENKUBE_API_KEY` is a deprecated, non-expiring credential. `AUTH_LEGACY_API_KEY` (`auth.legacyApiKey`) decides
what it may do:

| Value | The bootstrap key |
|---|---|
| `admin` (default) | is a full-admin Bearer key, as before; `GET /auth/me` reports `deprecated_credential: true` |
| `metrics` | may only scrape metrics (API, controller and worker) |
| `disabled` | is rejected everywhere (`401`) |

- **New installs** in `session`, `oidc` and `proxy` modes no longer generate it: people sign in, machines use scoped
  keys and Prometheus uses the scrape token. `api_key` mode still generates it, because it is that mode's only
  administrator (`auth.legacyApiKey` must stay `admin` there; the chart and the API refuse anything else).
- **Upgraded installs** keep the key the hook generated earlier (the hook never removes a key), and it stays `admin`
  so that scripts using it keep working. `helm upgrade` prints a security warning while it is accepted as admin.

Migration:

1. Upgrade. The hook adds `GREENKUBE_METRICS_TOKEN` to the Secret `<release>` before the Pods roll, and the
   ServiceMonitors switch to it in the same release: Prometheus keeps scraping with no change on your side.
2. Find what still uses the bootstrap key: its audit actor is `api-key:<digest>`, and `GET /auth/me` with it answers
   `deprecated_credential: true`. Give each script a scoped key (**Settings > API keys**).
3. Set `auth.legacyApiKey: metrics` (or `disabled`) and upgrade. Then remove the `GREENKUBE_API_KEY` key from the
   Secret `<release>` if you want it gone for good.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/auth/login` | `{username, password, remember}`. Sets the session cookie; returns the principal and its CSRF token. |
| `POST` | `/api/v1/auth/logout` | Revokes the current session and clears the cookie (`204`). |
| `GET` | `/api/v1/auth/me` | The current principal (`kind`, `name`, `role`, `scopes`, `auth_source`, `must_change_password`, `csrf_token`, session). `401` when signed out. Behind a trusted proxy, starts the identity's session. |
| `POST` | `/api/v1/auth/password` | `{current_password, new_password}`. Revokes every session, issues a new cookie. |
| `GET` | `/api/v1/auth/sessions` | Active sessions of the signed-in user. |
| `DELETE` | `/api/v1/auth/sessions/{id}` | Signs out one session. |
| `GET`, `POST` | `/api/v1/auth/api-keys` | List or create keys (`admin`). |
| `DELETE` | `/api/v1/auth/api-keys/{id}` | Revoke a key (`admin`). |
| `GET` | `/api/v1/auth/providers` | Public: sign-in options (`mode`, `password`, `sso`) for the sign-in page. |
| `GET` | `/api/v1/auth/oidc/login` | Starts an OpenID Connect sign-in (`?return_to=/path`); redirects to the provider. |
| `GET` | `/api/v1/auth/oidc/callback` | Provider redirect: finishes the sign-in, sets the session cookie, redirects to the return path. |
| `GET`, `POST` | `/api/v1/auth/users` | List accounts, or create a local account `{username, password, role}` (`admin`). |
| `PATCH` | `/api/v1/auth/users/{id}` | `{role}` and/or `{disabled}` (`admin`). |

Until the initial password is changed, every other endpoint answers `403` with code `PASSWORD_CHANGE_REQUIRED`.

## Recovery

Once the initial password has been used, GreenKube erases it from `<release>-initial-admin` and `helm upgrade` no
longer points to it. To reset a forgotten password of a local account, run from the API Pod (which already holds the
database credentials):

```bash
kubectl -n greenkube exec deploy/greenkube-api -- greenkube auth reset-password admin
```

The command prints a new random password once, forces a change at the next sign-in, re-enables the account and
signs out all of its sessions. It never touches other accounts, and refuses single sign-on accounts (they have no
password).

As a last resort (no API Pod can start), delete the accounts and their sessions, then either set
`auth.admin.existingSecret` to a Secret holding a new password, or delete the emptied Secret `<release>-initial-admin`
and run `helm upgrade` (or sync) so that the hook issues a new initial password. Restart the API: on its next start it
creates the admin again. Single sign-on accounts are created again at their next sign-in.

```bash
kubectl -n greenkube exec -it greenkube-postgres-0 -- \
  psql -U greenkube -d greenkube -c "DELETE FROM auth_sessions; DELETE FROM auth_users;"
```

## Security review checklist (F14, F16)

| Item | Implementation |
|---|---|
| Cookie flags | `HttpOnly`, `SameSite=Strict`, `Path=/`, `Secure` (auto via HTTPS or trusted `X-Forwarded-Proto`, or always; always behind an HTTPS Ingress or HTTPRoute); `Strict-Transport-Security` on HTTPS |
| CSRF | Same-origin `Origin`/`Referer` and `X-CSRF-Token` on every cookie-authenticated mutation; wildcard CORS never credentialed |
| Rate limits | Per username and client IP (exponential lockout) and per IP (window), failed API keys per IP (window, scrapes apart), `429` with `Retry-After`, `greenkube_auth_failures_total`; the API rate limit is per client and per route template |
| Hashing | scrypt `N=2^15, r=8, p=1`, per-user salt, rehash on parameter change, dummy hash for unknown users |
| Session fixation | A new token at every sign-in and password change; a token presented at sign-in is revoked |
| Constant-time comparisons | API-key secrets, the bootstrap key and CSRF tokens use `hmac.compare_digest`; sessions are looked up by SHA-256 |
| Secrets in logs | Passwords, tokens and keys are never logged; key creation logs only the `gk_<id>` prefix and the actor |
| Storage | Session tokens and API-key secrets stored as SHA-256 only; passwords as scrypt hashes |
| Proxy identity headers | Accepted only when the TCP peer (recorded before `X-Forwarded-For` is applied, never the forwarded client) is in `TRUSTED_PROXY_IPS` (required in `proxy` mode, enforced at startup and in the chart); any identity or groups header from another peer is a `401` before any account or session is used |
| Proxy header families | One configured user header and its paired groups header per request; headers of a second family or a repeated user header are refused (`INVALID_IDENTITY`), groups are never merged across families; defaults are the single `X-Forwarded-User` / `X-Forwarded-Groups` family |
| Proxy sessions | A session started by the proxy is refused from any peer outside `TRUSTED_PROXY_IPS` |
| Header-only principals | May read; may not mutate without a session and its CSRF token (`CSRF_REJECTED`), so an ambient proxy cookie cannot be used cross-site |
| OIDC flow | Authorization code + PKCE `S256`; 256-bit `state`, `nonce` and verifier stored server-side, single use, ten-minute expiry; `state` bound to the browser by an `HttpOnly` `SameSite=Lax` cookie scoped to `/api/v1/auth/oidc/` |
| ID token | Signature verified against the JWKS (asymmetric algorithms only, never `none` or `HS*`); `iss`, `aud`, `azp`, `exp`, `iat`, `nbf`, `nonce`, `sub` checked; discovery must name the configured issuer over `https` |
| Open redirects | The post-sign-in return path is reduced to a local path (`//host`, absolute URLs, backslashes and `/api/` are refused) |
| Account takeover | SSO accounts are keyed by `proxy:<user>` / `oidc:<sub>`; an SSO identity never merges into an existing account (`ACCOUNT_CONFLICT`); SSO accounts have no password and fail password sign-in in constant time |
| Roles | Enforced server-side by the scope table and by `require_scope` on every mutating route; a test asserts that a viewer gets `403` on every mutating route of the app; the last active admin cannot be demoted or disabled |
| Group mapping | Malformed `AUTH_ROLE_GROUPS` or `AUTH_DEFAULT_ROLE` stop the API at startup; `defaultRole: none` refuses unmapped users |
| Initial password | Erased from the Secret once used; NOTES no longer points to it and prints `greenkube auth reset-password` instead |

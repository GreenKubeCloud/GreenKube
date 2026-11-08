# Authentication

GreenKube has two kinds of callers:

- **People** sign in to the dashboard with a username and password. The API issues a server-side session held in an
  `HttpOnly` cookie, so a reload, a new tab or (with **Keep me signed in**) a browser restart keeps you signed in.
- **Machines** (CI pipelines, Prometheus, scripts, the push API) send a scoped API key as
  `Authorization: Bearer gk_<id>_<secret>`.

Both resolve to one identity, the *principal*, which GreenKube records in audit events, plan approvals and ledger
entries: `user:<username>` for people, `apikey:<key name>` for scoped keys, and `api-key:<digest>` for the deprecated
bootstrap key.

This page covers the default mode, `config.apiAuthMode: session` (setting `API_AUTH_MODE`). The other modes are:

| Mode | Browser sign-in | API keys | Use |
|---|---|---|---|
| `session` (default) | yes | scoped keys and the deprecated bootstrap key | Every installation |
| `api_key` | no (`POST /auth/login` answers `409`) | scoped keys and the bootstrap key | Headless installations driven only by machines |
| `oidc` | reserved for SSO (roadmap F16); answers `503` | | |

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
   `helm upgrade` or Argo CD sync never issues a second initial password.

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
| `SESSION_COOKIE_SECURE` | `auth.session.secureCookie` | `auto` |
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
  `never` is for plain-HTTP tests only.
- Signing out revokes the session on the server. Changing a password revokes every session of the account and
  issues a new one for the current browser.
- **Account & sessions** (sidebar, or `/settings/account`) lists your active sessions and signs out any of them.
- Expired sessions, and sessions revoked more than a day ago, are purged daily.

Serve GreenKube over HTTPS for remote access (an Ingress with TLS): the dashboard shows a warning when it is reached
over plain HTTP from anything other than `localhost`. Behind an Ingress that terminates TLS, set
`config.trustedProxyIps` to the addresses of the Ingress controller so that the cookie is marked `Secure`.

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

Failed sign-ins are counted per username and per client IP (four times the per-username budget). Past the budget,
the username is locked for one rate-limit window, doubling at every further exhaustion up to 15 minutes; a
successful sign-in resets it. Locked attempts answer `429` with `Retry-After`. Errors never say whether a username
exists, and an unknown username costs the same password-hashing time as a known one.

Counters live in each API process: with several replicas, an attacker gets at most the budget times the number of
replicas. `greenkube_auth_failures_total{reason}` counts `bad_credentials`, `rate_limited`, `invalid_session`,
`invalid_api_key`, `csrf` and `forbidden_scope` rejections.

### Passwords

Passwords are hashed with scrypt from the Python standard library (`N=2^15`, `r=8`, `p=1`, 16-byte random salt,
32-byte key), stored as `scrypt$N$r$p$salt$hash`. A hash with weaker parameters is upgraded at the next successful
sign-in. Passwords need at least 12 characters (at most 256) and must differ from the username.

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
| `read` | Every `GET` under `/api/v1` |
| `write` | Mutations: scans, ignore/restore, mark applied, plan previews, binding resolution |
| `approve` | Plan approval (`POST /api/v1/automation/plans/{id}/approve`), which opens pull requests |
| `signals:write` | The push API for recommendation signals (`/api/v1/signals`, roadmap G6) |
| `metrics:read` | Only the Prometheus endpoint `/prometheus/metrics` |
| `admin` | Everything, including API keys |

User roles map to the same scopes: `viewer` (`read`, `metrics:read`), `operator` (adds `write` and `approve`) and
`admin` (everything). The bootstrap account is an `admin`; more accounts and SSO roles arrive with roadmap F16.

### Prometheus

Create a key with only the `metrics:read` scope, store it in a Secret in the namespace of the ServiceMonitor, and
reference it:

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

That key is rejected everywhere else (`403` on `/api/v1/recommendations`, for example).

### The deprecated bootstrap key

`GREENKUBE_API_KEY` (generated into the Secret `<release>`, or `secrets.apiKey`) still works as an admin Bearer key
for one more minor release; `GET /auth/me` reports it with `deprecated_credential: true`. Replace it with scoped keys,
then stop using it. The ServiceMonitor uses it while `monitoring.serviceMonitor.bearerTokenSecret.name` is empty.
The controller and automation-worker metrics endpoints still accept only this key.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/auth/login` | `{username, password, remember}`. Sets the session cookie; returns the principal and its CSRF token. |
| `POST` | `/api/v1/auth/logout` | Revokes the current session and clears the cookie (`204`). |
| `GET` | `/api/v1/auth/me` | The current principal (`kind`, `name`, `role`, `scopes`, `must_change_password`, `csrf_token`, session). `401` when signed out. |
| `POST` | `/api/v1/auth/password` | `{current_password, new_password}`. Revokes every session, issues a new cookie. |
| `GET` | `/api/v1/auth/sessions` | Active sessions of the signed-in user. |
| `DELETE` | `/api/v1/auth/sessions/{id}` | Signs out one session. |
| `GET`, `POST` | `/api/v1/auth/api-keys` | List or create keys (`admin`). |
| `DELETE` | `/api/v1/auth/api-keys/{id}` | Revoke a key (`admin`). |

Until the initial password is changed, every other endpoint answers `403` with code `PASSWORD_CHANGE_REQUIRED`.

## Recovery

If nobody can sign in any more (forgotten admin password), delete the accounts and their sessions, then either set
`auth.admin.existingSecret` to a Secret holding a new password, or delete the emptied Secret
`<release>-initial-admin` and run `helm upgrade` (or sync) so that the hook issues a new initial password. Restart
the API: on its next start it creates the admin again.

```bash
kubectl -n greenkube exec -it greenkube-postgres-0 -- \
  psql -U greenkube -d greenkube -c "DELETE FROM auth_sessions; DELETE FROM auth_users;"
```

## Security review checklist (F14)

| Item | Implementation |
|---|---|
| Cookie flags | `HttpOnly`, `SameSite=Strict`, `Path=/`, `Secure` (auto via HTTPS or trusted `X-Forwarded-Proto`, or always) |
| CSRF | Same-origin `Origin`/`Referer` and `X-CSRF-Token` on every cookie-authenticated mutation; wildcard CORS never credentialed |
| Rate limits | Per username and per IP, exponential lockout, `429` with `Retry-After`, `greenkube_auth_failures_total` |
| Hashing | scrypt `N=2^15, r=8, p=1`, per-user salt, rehash on parameter change, dummy hash for unknown users |
| Session fixation | A new token at every sign-in and password change; a token presented at sign-in is revoked |
| Constant-time comparisons | API-key secrets, the bootstrap key and CSRF tokens use `hmac.compare_digest`; sessions are looked up by SHA-256 |
| Secrets in logs | Passwords, tokens and keys are never logged; key creation logs only the `gk_<id>` prefix and the actor |
| Storage | Session tokens and API-key secrets stored as SHA-256 only; passwords as scrypt hashes |

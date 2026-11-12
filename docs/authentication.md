# Authentication

GreenKube has two kinds of callers:

- **People** sign in to the dashboard with a username and password. The API issues a server-side session held in an
  `HttpOnly` cookie, so a reload, a new tab or (with **Keep me signed in**) a browser restart keeps you signed in.
- **Machines** (CI pipelines, Prometheus, scripts, the push API) send a scoped API key as
  `Authorization: Bearer gk_<id>_<secret>`.

Both resolve to one identity, the *principal*, which GreenKube records in audit events, plan approvals and ledger
entries: `user:<username>` for people, `apikey:<key name>` for scoped keys, and `api-key:<digest>` for the deprecated
bootstrap key.

The mode is `config.apiAuthMode` (setting `API_AUTH_MODE`):

| Mode | Browser sign-in | API keys | Use |
|---|---|---|---|
| `session` (default) | local accounts (username and password) | scoped keys and the deprecated bootstrap key | Every installation |
| `proxy` | single sign-on through an authentication proxy (oauth2-proxy, Authentik, Pomerium), plus local accounts | same | Clusters that already front their tools with an auth proxy |
| `oidc` | native OpenID Connect (**Sign in with ...**), plus local accounts | same | Any OpenID Connect provider (Keycloak, Dex, Entra ID, Okta, Google, GitLab) |
| `api_key` | no (`POST /auth/login` answers `409`) | scoped keys and the bootstrap key | Headless installations driven only by machines |

Every browser mode ends in the same server-side session, cookie and CSRF protection described below; only the way
the user proves who they are differs. See [Single sign-on](#single-sign-on) and [Roles](#roles).

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
exists, and an unknown username costs the same password-hashing time as a known one. A wrong current password on
`POST /api/v1/auth/password` counts as a failed sign-in for that user, so a stolen session cookie does not allow
unthrottled guessing either.

The per-username lockout applies before the password is checked, so anyone who can reach the sign-in page can keep a
known username (for example `admin`) locked by sending wrong passwords. This is the price of resisting distributed
guessing. API keys keep working while a username is locked; to limit exposure, keep the sign-in page off the public
internet. Behind an Ingress or load balancer, set `config.trustedProxyIps` so GreenKube sees each client's address
(the rightmost `X-Forwarded-For` entry that is not a trusted proxy); otherwise every user shares the proxy's per-IP
budget.

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
    userHeaders: X-Auth-Request-User,X-Forwarded-User      # defaults
    groupsHeaders: X-Auth-Request-Groups,X-Forwarded-Groups
```

- GreenKube accepts identity headers **only** from a peer listed in `config.trustedProxyIps`. A request carrying an
  identity header from any other address is rejected with `401 UNTRUSTED_PROXY_HEADER` (and counted as
  `greenkube_auth_failures_total{reason="untrusted_proxy_header"}`), whatever cookie or account it also carries. The
  chart refuses to render `proxy` mode without `trustedProxyIps`, and the API refuses to start without it.
- List only the proxy's own addresses: every peer in that list can claim any identity. Make sure the proxy strips
  these headers from incoming client requests (oauth2-proxy, Authentik and Pomerium do) and that the GreenKube
  Service is not reachable around the proxy, for example with `monitoring.networkPolicy`.
- The first `GET /api/v1/auth/me` of an identity starts its session (the dashboard does this on load). Reads work at
  once; mutations need the session's CSRF token, so a header-only request is refused with `CSRF_REJECTED` and the
  dashboard refreshes `/auth/me` and retries.
- oauth2-proxy: `--set-xauthrequest` (or `--pass-user-headers`) sends `X-Auth-Request-User` and
  `X-Auth-Request-Groups`. Authentik's proxy outpost sends `X-authentik-username` and `X-authentik-groups`: set
  `auth.proxy.userHeaders: X-authentik-username` and `auth.proxy.groupsHeaders: X-authentik-groups`. Pomerium:
  forward the claims as headers (`pass_identity_headers` / `jwt_claims_headers`) and point the two settings at them.
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
| `signals:write` | Pushing signals: `POST /api/v1/signals` ([Push API](sources/push.md)) and `POST /api/v1/sources/krr/reports` ([KRR](sources/krr.md)) |
| `metrics:read` | Only the Prometheus endpoint `/prometheus/metrics` |
| `admin` | Everything, including API keys |

User roles map to the same scopes: `viewer` (`read`, `metrics:read`), `operator` (adds `write`, `approve` and `plans:deliver`) and
`admin` (everything). See [Roles](#roles).

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
| Cookie flags | `HttpOnly`, `SameSite=Strict`, `Path=/`, `Secure` (auto via HTTPS or trusted `X-Forwarded-Proto`, or always) |
| CSRF | Same-origin `Origin`/`Referer` and `X-CSRF-Token` on every cookie-authenticated mutation; wildcard CORS never credentialed |
| Rate limits | Per username and per IP, exponential lockout, `429` with `Retry-After`, `greenkube_auth_failures_total` |
| Hashing | scrypt `N=2^15, r=8, p=1`, per-user salt, rehash on parameter change, dummy hash for unknown users |
| Session fixation | A new token at every sign-in and password change; a token presented at sign-in is revoked |
| Constant-time comparisons | API-key secrets, the bootstrap key and CSRF tokens use `hmac.compare_digest`; sessions are looked up by SHA-256 |
| Secrets in logs | Passwords, tokens and keys are never logged; key creation logs only the `gk_<id>` prefix and the actor |
| Storage | Session tokens and API-key secrets stored as SHA-256 only; passwords as scrypt hashes |
| Proxy identity headers | Accepted only from `TRUSTED_PROXY_IPS` (required in `proxy` mode, enforced at startup and in the chart); any identity header from another peer is a `401` before any account or session is used |
| Header-only principals | May read; may not mutate without a session and its CSRF token (`CSRF_REJECTED`), so an ambient proxy cookie cannot be used cross-site |
| OIDC flow | Authorization code + PKCE `S256`; 256-bit `state`, `nonce` and verifier stored server-side, single use, ten-minute expiry; `state` bound to the browser by an `HttpOnly` `SameSite=Lax` cookie scoped to `/api/v1/auth/oidc/` |
| ID token | Signature verified against the JWKS (asymmetric algorithms only, never `none` or `HS*`); `iss`, `aud`, `azp`, `exp`, `iat`, `nbf`, `nonce`, `sub` checked; discovery must name the configured issuer over `https` |
| Open redirects | The post-sign-in return path is reduced to a local path (`//host`, absolute URLs, backslashes and `/api/` are refused) |
| Account takeover | SSO accounts are keyed by `proxy:<user>` / `oidc:<sub>`; an SSO identity never merges into an existing account (`ACCOUNT_CONFLICT`); SSO accounts have no password and fail password sign-in in constant time |
| Roles | Enforced server-side by the scope table and by `require_scope` on every mutating route; a test asserts that a viewer gets `403` on every mutating route of the app; the last active admin cannot be demoted or disabled |
| Group mapping | Malformed `AUTH_ROLE_GROUPS` or `AUTH_DEFAULT_ROLE` stop the API at startup; `defaultRole: none` refuses unmapped users |
| Initial password | Erased from the Secret once used; NOTES no longer points to it and prints `greenkube auth reset-password` instead |

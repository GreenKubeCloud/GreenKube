### Added

- **Single sign-on through an authentication proxy** (F16): `config.apiAuthMode: proxy` trusts the identity headers
  of oauth2-proxy, Authentik or Pomerium (`X-Forwarded-User` / `X-Forwarded-Groups` by default, `auth.proxy.*`)
  only when the TCP peer is in `config.trustedProxyIps`, which the mode requires. Identity headers from any other
  peer are rejected with `401 UNTRUSTED_PROXY_HEADER`; a request mixing configured header families or repeating the
  user header is refused (`INVALID_IDENTITY`), and a proxy-started session is refused around the proxy. The first `GET /api/v1/auth/me` of an identity
  starts the same server-side session as a password sign-in; header-only requests may read but not mutate.
- **Native OpenID Connect sign-in** (F16): `config.apiAuthMode: oidc` with `auth.oidc.*` (issuer, client id,
  optional client secret Secret, scopes, username and groups claims, redirect URL, button label). Authorization code
  with PKCE `S256`, discovery, server-side single-use `state`, `nonce` and code verifier (migration 0094,
  `auth_oidc_logins`) bound to the browser by a `SameSite=Lax` cookie, ID tokens verified against the JWKS
  (RS/PS/ES/EdDSA only) with issuer, audience, authorized party, expiry and nonce checks. Routes
  `GET /api/v1/auth/oidc/login` and `/callback`; failures return to the sign-in page with a stable `sso_error` code.
- **Group to role mapping** (F16): `auth.roles.groups` (`AUTH_ROLE_GROUPS`, JSON role to groups) and
  `auth.roles.defaultRole` (`AUTH_DEFAULT_ROLE`, `none` refuses unmapped users). The highest matching role wins and
  is recomputed at every sign-in. SSO accounts are keyed by `proxy:<user>` / `oidc:<sub>` (migration 0093,
  `auth_users.auth_source` and `external_id`), have no password and never take over an existing account.
- **Users and roles administration** (F16, K11): `GET/POST /api/v1/auth/users` and `PATCH /api/v1/auth/users/{id}`
  (role, disable) for admins, and a **Users and roles** card on `/settings/account`. Disabling signs the user out; the
  last active admin can be neither demoted nor disabled.
- `GET /api/v1/auth/providers` (public) and a **Sign in with ...** button on the sign-in form; the account page shows
  the role, what it allows and the sign-in source, and hides the password form for SSO accounts (K11).
- `auth.localLogin` (`AUTH_LOCAL_LOGIN`): local accounts, including the break-glass admin, keep a password sign-in in
  SSO modes unless disabled.
- `greenkube auth reset-password [USERNAME]`: prints a new random password for a local account (forced change at the
  next sign-in), re-enables it and signs out its sessions (N11 follow-up).

### Changed

- Roles are enforced on each mutating route as well as by the global scope table (F16): plan creation, cancel,
  delete, rollback preview, ignore/restore, mark applied, remeasure and rollback-review dismissal need `write`
  (operator), plan approval needs `approve` (operator), delivery reports `plans:deliver`, the repository allowlist
  `admin`. A test asserts that a viewer gets `403` on every mutating route of the app.
- `helm install`/`upgrade` notes no longer point to the initial-admin Secret once its password has been used and
  erased; they print the `greenkube auth reset-password` command instead (N11).
- The initial-admin Secret and the bootstrap API key are also provided in `proxy` and `oidc` modes.
- `API_AUTH_MODE=oidc` no longer answers `503`; `proxy` is a new accepted value.
- `greenkube-api` records the TCP peer before applying `X-Forwarded-For` / `X-Forwarded-Proto` of
  `TRUSTED_PROXY_IPS`, so trusted-proxy checks (identity headers, `X-Forwarded-Host`) judge the connection, not the
  forwarded client (F16).

### Added
- **Sign in once: local accounts and server-side sessions (F14).** `API_AUTH_MODE=session` is implemented and is the new default (`config.apiAuthMode: session`). `POST /api/v1/auth/login|logout|password`, `GET /api/v1/auth/me` and `GET|DELETE /api/v1/auth/sessions[/{id}]`. Sessions are opaque 256-bit tokens in an `HttpOnly; SameSite=Strict; Path=/` cookie (`Secure` automatically over HTTPS or a trusted `X-Forwarded-Proto`, or always), stored as SHA-256 in `auth_sessions` and shared by every API replica (migration `0054`); 12 h sliding idle timeout, 7-day absolute lifetime, 30 days with "Keep me signed in"; a password change revokes every session; expired sessions are purged daily. Passwords are scrypt hashes (stdlib, per-user salt).
- **First admin bootstrap (F14, N11).** On a database without accounts the API creates `admin` (`auth.admin.username`) from `auth.admin.existingSecret`, or from the random password the credentials bootstrap hook writes once to the Secret `<release>-initial-admin`; the UI forces a password change at first sign-in and the API then erases the initial password from the Secret. `helm install` notes print the command that reads it.
- **CSRF and brute-force protection (F14).** Cookie-authenticated mutations need a same-origin `Origin`/`Referer` and the session's `X-CSRF-Token`; sign-in and wrong current passwords on password change are rate-limited per username and per IP with an exponential lockout (`AUTH_LOGIN_RATE_LIMIT`, `auth.login.rateLimit`, validated at startup); the client IP is the rightmost `X-Forwarded-For` hop that is not in `TRUSTED_PROXY_IPS`; at most two scrypt hashes run at once per API process; new metric `greenkube_auth_failures_total{reason}`.
- **Scoped API keys for machines (F15).** `GET|POST /api/v1/auth/api-keys`, `DELETE /api/v1/auth/api-keys/{id}`; keys `gk_<id>_<secret>` are shown once and stored as SHA-256 (table `api_keys`, migration `0055`) with scopes `read`, `write`, `approve` (plan approval and `POST /api/v1/recommendations/{id}/apply-pr`), `signals:write`, `metrics:read`, `admin`, optional expiry and `last_used_at`. The ServiceMonitor can scrape with a `metrics:read` key (`monitoring.serviceMonitor.bearerTokenSecret`).
- **Principal identity (F14).** Audit actors are `user:<name>` for signed-in users and `apikey:<name>` for scoped keys (the bootstrap key keeps `api-key:<digest>`).
- **Authentication UI (K11).** Password-manager friendly sign-in page, forced first-login password change, account menu with active sessions (revoke) and sign-out (`/settings/account`), API keys page with show-once creation and revocation (`/settings/api-keys`), and a warning banner on remote plain-HTTP access.
- Settings `SESSION_COOKIE_SECURE` (`auto`/`always`/`never`), `AUTH_SESSION_IDLE_TIMEOUT`, `AUTH_SESSION_ABSOLUTE_LIFETIME`, `AUTH_SESSION_REMEMBER_LIFETIME`, `AUTH_LOGIN_RATE_LIMIT`, `AUTH_ADMIN_USERNAME`, `AUTH_ADMIN_PASSWORD`, `AUTH_INITIAL_ADMIN_SECRET`; Helm `auth.*` and `config.trustedProxyIps` (`TRUSTED_PROXY_IPS`).
- New guide `docs/authentication.md`, including the F14 security review checklist.

### Changed
- **The deprecated bootstrap key.** `GREENKUBE_API_KEY` is still accepted as an admin Bearer key (deprecated, `deprecated_credential` in `/auth/me`); people sign in with accounts and machines use scoped keys (F15).
- **Settings page and connectivity popup are read-only (A13).** Data sources are configuration as code: they show health and copy-paste Helm snippets instead of edit forms.
- The API client sends same-origin credentials and the CSRF token instead of a Bearer key; `401` opens the sign-in page (K11).
- The credentials bootstrap hook also runs with `secrets.existingSecret` when it must create the initial-admin Secret; it then never reads or writes the user's Secret.
- `SESSION_COOKIE_SECURE` changed from a reserved boolean to `auto`/`always`/`never` (`true`/`false` map to `always`/`never`).

### Fixed
- **Runtime data-source edits reached only the API process and drifted from Git (A13, BUG-11, BUG-60).** `POST /api/v1/config/services` now answers `410 Gone`; the API no longer patches the release Secret.

### Removed
- The per-tab `sessionStorage` API-key sign-in of the dashboard (F13 stopgap), replaced by sessions (K11).
- `patch_k8s_secret`, the `GREENKUBE_SECRET_NAME` ConfigMap entry and the `<release>-secret-writer` Role and RoleBinding that let the API patch the release Secret (A13, N8, BUG-60). The API's only Secret access is `get`/`patch` on `<release>-initial-admin`.

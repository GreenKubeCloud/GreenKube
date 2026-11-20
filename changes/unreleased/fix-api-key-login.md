### Fixed
- `apiAuthMode: api_key`: the sign-in page showed no field at all (field report F-09). It now asks for an API key (the bootstrap key or a scoped `gk_` key), checked by the new `POST /api/v1/auth/api-key-login` and kept by the server in the `HttpOnly; SameSite=Strict` cookie, never in browser storage; the key is re-checked on every request and cookie-authenticated mutations need the CSRF token. `GET /api/v1/auth/providers` gains `api_key`. The "initial admin password" hint is no longer shown in this mode.

### Changed
- Upgrade note: a 0.3.1 install in `api_key` mode keeps browser access with its key, or moves to `session` for accounts (docs/upgrading.md).

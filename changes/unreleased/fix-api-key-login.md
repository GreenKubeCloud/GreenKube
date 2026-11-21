### Fixed
- `apiAuthMode: api_key`: the sign-in page showed no field at all (field report F-09). It now asks for an API key (the bootstrap key or a scoped `gk_` key), checked by the new `POST /api/v1/auth/api-key-login` and exchanged for a signed, expiring `HttpOnly; SameSite=Strict` cookie (`v1.<kid>.<exp>.<mac>`, 12 hours or 30 days with "keep me signed in") that never contains the key. The signing key derives from `GREENKUBE_API_KEY`: rotating it signs browsers out. The key is re-checked on every request, needs the `read` scope, and the sign-in needs HTTPS or localhost; cookie-authenticated mutations need the CSRF token. Signed-in browsers get the account page and Sign out. `GET /api/v1/auth/providers` gains `api_key`. The "initial admin password" hint is no longer shown in this mode.

### Changed
- Upgrade note: a 0.3.1 install in `api_key` mode keeps browser access with its key, or moves to `session` for accounts (docs/upgrading.md).

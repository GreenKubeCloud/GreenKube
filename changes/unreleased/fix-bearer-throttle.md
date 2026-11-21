### Fixed
- Failed `Authorization: Bearer` authentications and rejected API-key browser cookies are now throttled per client IP (four times `AUTH_LOGIN_RATE_LIMIT`, no exponential lockout), answering `429 TOO_MANY_ATTEMPTS` with `Retry-After`; before, only the global `API_RATE_LIMIT` bounded online guessing of an operator-chosen bootstrap key. Prometheus scrapes on `/prometheus/metrics` use a separate bucket, successful authentications never consume the budget, and clients sharing one address share it. Refusals are counted as `greenkube_auth_failures_total{reason="key_rate_limited"}`.
- The controller and worker metrics listeners throttle failed scrape credentials per TCP peer the same way.
- In `api_key` mode, a stale browser cookie is no longer counted as a failure on an open development instance or on the public sign-in routes; it is cleared instead, so a browser can always sign in again.

### Changed
- The controller and worker metrics listeners no longer accept the bootstrap key `GREENKUBE_API_KEY` once a scrape token (`GREENKUBE_METRICS_TOKEN`) is configured. The chart's ServiceMonitors already send the scrape token whenever it exists; a scraper of your own that sends the bootstrap key to these listeners must switch to the scrape token.

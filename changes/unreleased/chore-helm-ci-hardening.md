### Added
- **Helm: credentials bootstrap hook (N1, BUG-58).** A `<release>-credentials-bootstrap` pre-install/pre-upgrade hook (an Argo CD PreSync hook) creates the generated `GREENKUBE_API_KEY` and bundled PostgreSQL password once, in the Secret `<release>`, and afterwards only adds missing keys. The Secret is outside the Helm release and Argo CD tracking, so upgrades and syncs never rotate it and `helm uninstall` keeps it with the PostgreSQL volume. The hook refuses to generate a new password for a retained PostgreSQL volume and prints the recovery command.
- **Helm: install notes print the exact API-key command and the setup URL (N11).** The command targets the Secret that actually holds the key (generated, values or `secrets.existingSecret`).
- **CI: Dependabot** for uv, npm, GitHub Actions and Docker, targeting `dev` (O6).
- **CI: render-stability check** — two `helm template` runs must produce identical output for the standalone and production profiles, with automation on and off, and with credentials supplied in values (N1).
- **Release: Artifact Hub changes list generated from the CHANGELOG** by `scripts/artifacthub_changes.py`, run by `scripts/release.sh` and the release workflow (N10).

### Changed
- **Helm: Argo CD-safe Secrets (N1, BUG-58).** Rendering no longer uses `lookup` or `randAlphaNum`, so `helm template` output is a pure function of the values. Credentials supplied in values are rendered into a regular (non-hook) Secret `<release>-values`; each Pod key is projected from exactly one Secret. `secrets.existingSecret` is documented as the GitOps path. Upgrades from 0.3.x keep the existing Secret `<release>` and its API key and password unchanged. **Breaking for `rbac.create=false`:** installs that relied on a generated API key or PostgreSQL password no longer render. Before upgrading, set `secrets.existingSecret=<release>` (the Secret that 0.3.x created) or supply `secrets.apiKey` and `postgres.auth.password`.
- **Helm: `config.boavizta.url` defaults to `""` (N1).** GreenKube then uses the public Boavizta API. As with the Prometheus and OpenCost URLs, a URL saved on the Settings page now survives Pod restarts, and a URL set in values takes precedence. With `secrets.existingSecret`, a URL set in values is passed through the ConfigMap, as in 0.3.x.
- **Helm: CORS is same-origin by default (N4, BUG-59).** `config.corsOrigins` defaults to `""` instead of `*`.
- **Helm: Artifact Hub category is `integration-delivery`** and the changes list describes the current release (N10).
- **Helm: `.helmignore` excludes the helm-unittest suites** from the packaged chart.
- **CI (O6):** the coverage gate is raised from 70% to 80%; the Python job timeout is 20 minutes; the release is gated on frontend tests and a frontend build; the CI workflow is read-only.
- **Release: `greenkube/greenkube:latest` is pushed for the newest release (O6, BUG-62).** Pre-release tags such as `v0.5.0-rc1` do not count when the workflow picks the newest release.

### Fixed
- **API: a wildcard CORS origin is never combined with credentials (N4, BUG-59).** `CORS_ORIGINS=*` now allows any origin without `Access-Control-Allow-Credentials`; credentials are allowed only for explicitly listed origins.
- **Helm: the PostgreSQL credential check no longer blocks the first Argo CD sync (N1).** The pre-upgrade check skips when the credentials or the database do not exist yet, and fails only on a real authentication error.
- **Helm: the retained-volume guard works under Argo CD (N1, BUG-58).** It moved from a render-time `lookup` to the bootstrap hook.
- **Helm: the PostgreSQL credential check validates a newly set `postgres.auth.password` (N1).** It reads the new password from a short-lived pre-upgrade hook Secret, because `<release>-values` is updated only after the hooks run.

### Removed
- **CI: the dead README-badge step** and its `contents: write` permission (O6).
- **Repository: the stale, tracked `coverage.json`** is untracked and ignored (O6).

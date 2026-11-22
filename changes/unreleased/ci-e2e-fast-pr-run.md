### Changed
- The end-to-end loop runs in full (Argo CD then Flux, measurement verdict included, about 45 minutes) only nightly, on demand and before a release. Pull requests to `main` get a fast run of about 15 minutes: Argo CD only, stopping once the change is applied and its measurement has started. Pull requests to `dev` no longer run it.

### Changed

- **Automation page layout:** workload coverage is one panel with a coverage bar and a compact count per binding status instead of seven separate cards; plan automation readiness metrics share one panel; the header, page gutter and section padding fit phone widths; each section is a labelled region.

### Fixed

- **One card per GitOps controller (USER BUG 3):** the Automation & GitOps page rendered one card per discovered CRD (one for Argo CD, six for Flux), so Flux filled the discovery section. It now shows exactly one card each for Argo CD and Flux, with the same visual weight, an overall status (`Detected`, `Not detected`, `Degraded` naming the unreadable kinds, or `Unknown` when the status is missing) and, inside the Flux card, the Kustomizations, HelmReleases and Git/OCI/Helm repository and Bucket kinds with their individual status. Grouping happens in the browser; the `/api/v1/gitops/status` contract is unchanged.

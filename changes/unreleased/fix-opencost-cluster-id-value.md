### Fixed
- `values.yaml` listed `clusterId` under `config.prometheus`, where the chart never reads it; it now sits under `config.opencost.clusterId`, the key that sets `OPENCOST_CLUSTER_ID`.

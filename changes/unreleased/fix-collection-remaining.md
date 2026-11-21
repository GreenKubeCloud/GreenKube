### Added
- `GET /api/v1/health` reports `collection.container_usage_source` (`prometheus`, `metrics_server`, `mixed` or null): `metrics_server` means container usage is sampled from the Metrics API because cAdvisor exports pod-level series only, for example on cri-dockerd (COL-05).
- Container rows store the largest working-set scrape inside each step in `memory_usage_max_bytes`; rightsizing percentiles, the observed maximum and the memory risk checks use it (COL-12).

### Changed
- Calculation version `+range.9`. Rows stored before keep their values; re-collect a window inside Prometheus retention to apply the changes below.
- CPU of a container adds its consecutive instances inside the rate window (restart, crash loop, pod recreated under the same name, told apart by cAdvisor's `name` label); duplicates of one instance still count once (COL-11).
- `memory_usage_bytes` is the working-set average over the step (`avg_over_time`), not the instant value at the step boundary (COL-12).
- The node cloud provider comes from `spec.providerID`, else from provider label prefixes, else from `CLOUD_PROVIDER` (only for nodes nothing identifies, so hybrid clusters keep each node's provider); region names are no longer used, and unidentified Linode, Oracle, IBM and Alibaba nodes stay `unknown` by default. The on-prem aliases (`on-prem`, `on-premise`, `on-premises`, `onprem`, `on_prem`) are never looked up in a cloud catalogue or the Boavizta cloud API (COL-15).
- Pod owners follow the controller chain: a ReplicaSet resolves to its Deployment or Argo Rollout (or stays a ReplicaSet), and a Job created by a CronJob resolves to the CronJob. The ClusterRole can now list `batch/jobs`. Owners of pods known only from kube-state-metrics follow the same chain through `kube_replicaset_owner` and `kube_job_owner`. When ReplicaSets cannot be listed, the name rule is kept and a warning is logged (COL-16).
- Recommendation evidence, the memory `restart_history` risk and the stability score count the restarts inside the window (increases of the lifetime counter between rows), not the lifetime restart counter (COL-20).
- A closed `DatabaseManager` raises `DatabaseClosedError` on use instead of silently reopening a connection; `connect()` and `setup_*()` still open it explicitly (roadmap A.9).

### Fixed
- Migration 0270 repairs stored pod rows with neither owner nor requests (`metadata_source` NULL or `missing`) from the last earlier row of the same pod that has metadata, within one continuous run of rows of the same incarnation (no gap over three steps, no new `pod_snapshots` version in between); repaired rows say `metadata_source = repaired` (`repaired_partial` when the source had no owner), get a `+metadata-repaired` model version, so `greenkube recompute` re-runs their allocation, and get new row ids, so the workload usage rollup re-rolls their hours. The migration is linear in the table size. Container rows with owner NULL take their pod's owner (COL-01).
- The range collector reads only the labels the PromQL catalog groups by; the test fake Prometheus evaluates `or`, nested aggregations and `*_over_time` windows (COL-21).

### Added
- `COLLECTION_K8S_LIST_MAX_FAILURES` (`config.collection.k8sListMaxFailures`, default 3): after this many consecutive failed Kubernetes pod or node lists for the same window, the window is stored in degraded mode (rows without history flagged with reason `k8s_list_failed`) instead of being retried until it becomes a gap. Counted in `greenkube_collection_degraded_windows_total{list}`, with the alert `GreenKubeCollectionDegraded`.
- `pod_snapshots` records the pod UID and the cluster identity (migration 0121); `node_snapshots_scd.last_seen` records when an unchanged node version was last confirmed.

### Fixed
- A re-collection no longer deletes stored container rows: a pod row rebuilt from its stored row keeps the stored container rows (requests of init containers stay on the pod row), and a pod for which the pass builds no container row keeps its stored pod and container rows together, so pod joules still equal the sum of container joules.
- Rows without provenance (written before calculation version `+range.4`) are not reused as stored metadata.
- A sample older than the pod's first observation (first-install backfill, downtime) takes its metadata from kube-state-metrics when available; otherwise a pod whose owner reuses names (StatefulSet, bare pod) is flagged `pod_metadata_uncertain`.
- kube-state-metrics queries are filtered to the pods concerned (chunks of at most 50 names and about 4 KB) and include native sidecars and containers, with or without requests; a failed chunk no longer discards the others and its pods are flagged `kube_state_metrics_unavailable`; stored rows are no longer read for pods kube-state-metrics resolved.
- `greenkube_collection_degraded_windows_total` exposes every `list` label from start-up, so the first degraded window raises `GreenKubeCollectionDegraded`.
- `COLLECTION_K8S_LIST_MAX_FAILURES` counts attempts per window start, so a window retried with a later end is the same window.
- Pods never observed live by a collection take their owner, requests, limits and containers from kube-state-metrics at each step (`metadata_source = kube_state_metrics`, WP-01.1 step 2); without kube-state-metrics they stay `missing`.
- A removed node whose snapshot never changed for more than `NODE_DATA_MAX_AGE_DAYS` no longer falls back to the default zone and PUE: the age counts from the end of the version or its last confirmation, not from `valid_from`.
- A pod deleted during a controller downtime longer than one hour keeps its metadata: pods of the window are looked up in the history by name without lower bound.
- A spec rolled back A, B, A resolves to A after the rollback: the pod history stores one version per observation interval instead of one per spec hash.
- A row re-collected after its pod and history are gone takes the owner, requests and containers of the stored row and the allocation is re-run with them (`metadata_source = stored_row`); a row with `metadata_source = missing` never replaces a stored row that had metadata (the whole stored row is kept, not only its owner and requests). The collection job rewrites the container rows of each pod row in the same transaction, so pod joules equal the sum of their container joules.
- A pod recreated with a new UID (or without UID to tell) during an unobserved gap is flagged with reason `pod_metadata_uncertain` instead of silently taking the old pod's metadata.
- Pod and node lists are paginated (500 objects per page) so large clusters do not time out on one unbounded list.
- The Prometheus exposition no longer exports a NULL CPU or memory usage as 0 (the series is omitted), and the time-series aggregate reports NULL usage for a bucket without any sample.
- The zombie analyzer no longer flags a workload without any CPU sample over the lookback (low energy is unmeasured, not evidence of idleness).

### Changed
- Calculation version `+range.5`. `+range.4` rows are not rewritten; a re-collection of their window applies the new rules.

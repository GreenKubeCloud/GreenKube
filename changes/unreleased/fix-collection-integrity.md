### Added
- Pod metadata history (`pod_snapshots`, migration 0120): every collection records the owner, requests, limits and containers of the live pods, one version per spec.
- Raw pod and container rows record `node_source` (`snapshot`, `current_api`, `unknown`) and `metadata_source` (`k8s_snapshot`, `missing`).

### Fixed
- Pods without a CPU sample at a step (first step, short-lived or crash-looping containers, memory-only series) now get their row when they have a memory sample or the Running phase in kube-state-metrics: `cpu_usage_millicores` NULL, reason `no_cpu_sample`, cost and requests kept (COL-03, steps 1, 2).
- A failed carbon-intensity lookup fails the collection window (logged at ERROR, retried, watermark unchanged) instead of silently dropping every pod row of the node and step (COL-08).
- A failed Kubernetes pod list fails the collection window (retried, watermark unchanged) unless a pod snapshot newer than one step exists; a failed node list fails it unless node snapshots exist. Counted in `greenkube_collection_k8s_list_errors_total` (COL-07).
- Historical rows take node zone, Electricity Maps zone, provider and PUE, instance type and embodied profile from the node snapshot valid at the sample time, so rows of a node that left the cluster no longer fall back to the default zone and PUE with zero embodied carbon (RECON-05, COL-01).
- Historical rows take owner, requests and container list from the pod history at the sample time, so a pod deleted before its window was collected keeps its owner, requests and container rows (COL-01).
- A re-collected row without pod metadata no longer overwrites the stored owner and requests (COL-01).

### Changed
- Calculation version `+range.4`. Rows stored before keep the node and pod state of their collection time and are not rewritten.

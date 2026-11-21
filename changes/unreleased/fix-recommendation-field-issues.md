### Added
- Source-run diagnostic `static_pods_excluded` (memory rightsizing source): the number of static (mirror) pods left out of the analyzers.
- `WorkloadUsageSummary.replicas_latest`: pods seen at the latest step of the target's last observed hour (a pod a rollout replaced earlier in that hour is not counted), computed by the rollup SQL (SQLite and PostgreSQL) and the reference implementation. Migration 0210 adds `workload_usage_hourly.latest_pod_count` (pods at each node row's latest sample); hours rolled before it read `pod_count`.
- `WorkloadUsageSummary.cpu_p95_hour_max` / `memory_p95_hour_max`: the highest hourly p95 over the lookback.
- `get_recommendations_by_ids` on the recommendation repositories: one `WHERE id IN (...)` query (SQLite) / `= ANY(...)` (PostgreSQL).

### Changed
- Eligibility evaluates the policy gates that depend only on the recommendations (risk, confidence, change-freeze windows, sample count and coverage) with the same function as plan creation (`evaluate_recommendation_gates`), over every recommendation of the typed action. Blocker messages for a missing risk level or confidence now use the policy evaluator's wording.

### Fixed
- F-04 / REC-09: static pods of kubeadm, kind, minikube and k3s control planes (owner kind `Node`, annotation `kubernetes.io/config.mirror`) no longer receive rightsizing, HPA or any other recommendation. No source may store a recommendation whose owner kind is `Node`; recommendations stored by earlier versions are marked `stale` with the reason `static_pod` at the next scan (never deleted). The pod collector attributes a mirror pod without a Node owner to its node.
- F-07: a recommendation shown with `can_open_pr: true` is no longer refused by plan creation with `policy_evidence_missing` (`sample_count_coverage`) or `policy_risk_exceeded` because of another recommendation of the same action, invalid coverage evidence, or a confidence stored only in the evidence.
- F-08: `singleton_workload` was raised on every multi-replica workload scanned from the usage rollup (its baseline rows carry one pseudo pod per target). The replica count now comes from the pods at the latest step of the last observed hour, so a rollout in that hour does not hide a real singleton. Stored risk factors are corrected at the next scan that re-emits the recommendation.
- The per-replica p95 guard (`replica_p95_exceeds_proposal`) read hourly averages of the rollup's pseudo pod, so a spiky replica no longer flagged a proposal below its peaks; on the rollup path it now uses the container's highest hourly p95.
- Eligibility reads the siblings of an action from the unfiltered active list and then one batched query, instead of one query per sibling.

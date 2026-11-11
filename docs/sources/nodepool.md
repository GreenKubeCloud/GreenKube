# NodePools and autoscalers (`greenkube.nodepool`)

GreenKube reads the cluster's autoscalers for two purposes (roadmap G5): to estimate how much released capacity really becomes removed nodes (the realization factor), and to advise on Karpenter NodePool policies.

## Realization context (not recommendations)

The controller reads, read-only:

- the Cluster Autoscaler status ConfigMap `kube-system/cluster-autoscaler-status`;
- Karpenter NodePools (consolidation policy, disruption budgets) and NodeClaims (claim → pool);
- Karpenter events `DisruptionBlocked` (a blocker: the pool's factor is halved, with the event in the reasons) and `Unconsolidatable` (reported in the reasons, factor unchanged) on NodePools, NodeClaims and Nodes;
- `do-not-disrupt` / `safe-to-evict` annotations and blocking PodDisruptionBudgets.

These feed the [RealizationContext](../methodology/impact-model.md#realization-factor). Events that cannot be read (RBAC) are reported as "Karpenter events could not be read", never as "no blocker". Karpenter's Prometheus metrics (`karpenter_nodepools_cost_total`, `karpenter_voluntary_disruption_decisions_total`) are not read yet.

## NodePool advice

Besides its consolidation recommendation for under-utilized pools, the source emits **policy signals**. They are high risk (`evidence.data.risk_class: high`) and advisory only (`AdvisoryProposal`): no v1 recommendation and no pull request until GreenKube supports NodePool changes (roadmap H7). Each carries the suggested patch in `evidence.data.suggested_patch`.

| Kind | When | Suggested patch |
|---|---|---|
| `NODEPOOL_CONSOLIDATION_POLICY` | `consolidationPolicy: WhenEmpty` | `WhenEmptyOrUnderutilized` |
| `NODEPOOL_CONSOLIDATE_AFTER` | `consolidateAfter` above 1 hour, or `Never` | `5m` |
| `NODEPOOL_CAPACITY_TYPE` | only `on-demand` capacity (explicitly or by default) and every workload observed on the pool's nodes in the scan is stateless (no StatefulSet); never when nothing was observed | allow `spot` |
| `NODEPOOL_ARCHITECTURE` | only `amd64` (explicitly or by default) | allow `arm64`, **only if every image on the pool is multi-arch**, which GreenKube cannot verify |

Requirements are read from `spec.template.spec.requirements`. Policy signals are emitted by cluster-wide scans only.

## Permissions

`get`/`list` on `karpenter.sh` NodePools and NodeClaims, and `list` on core `events` (both in the chart's ClusterRole).

## Configuration

`config.recommendations.karpenterEnabled` or `sources.settings["greenkube.nodepool"].enabled`. The realization factor can be pinned with `impact.realizationFactor`.

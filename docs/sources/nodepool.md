# Karpenter and the Cluster Autoscaler

> The file keeps its historical name (`nodepool.md`) so existing links work. Karpenter and the Cluster Autoscaler are not recommendation sources: GreenKube reads them only to estimate the realization factor. The NodePool consolidation heuristic (`greenkube.nodepool`) and its NodePool policy signals were removed in 0.4.

GreenKube reads the cluster's autoscalers to estimate how much released capacity really becomes removed nodes: the [realization factor](../methodology/impact-model.md#realization-factor), from Karpenter or the Cluster Autoscaler (roadmap G5). They never produce recommendations.

Code: `src/greenkube/collectors/autoscaler_collector.py` and `src/greenkube/impact/realization.py`.

## Realization context

The controller reads, read-only (Cluster Autoscaler and Karpenter):

- the Cluster Autoscaler status ConfigMap `kube-system/cluster-autoscaler-status`;
- Karpenter NodePools (consolidation policy, disruption budgets) and NodeClaims (claim → pool);
- Karpenter events `DisruptionBlocked` (a blocker: the pool's factor is halved, with the event in the reasons) and `Unconsolidatable` (reported in the reasons, factor unchanged) on NodePools, NodeClaims and Nodes;
- `do-not-disrupt` / `safe-to-evict` annotations and blocking PodDisruptionBudgets.

These feed the [RealizationContext](../methodology/impact-model.md#realization-factor). Events that cannot be read (RBAC) are reported as "Karpenter events could not be read", never as "no blocker". When `PROMETHEUS_URL` is set, Karpenter's metrics are read too (no extra permission): `karpenter_voluntary_disruption_decisions_total` (consolidation decisions over 7 days, listed as evidence), `karpenter_voluntary_disruption_eligible_nodes` (no decision while nodes are eligible halves the factor of consolidating pools) and `karpenter_nodepools_cost_total` (cost increase per pool, recorded as `observed_cost`). Metrics that are not exported change nothing.

## Permissions

`get`/`list` on `karpenter.sh` NodePools and NodeClaims, and `list` on core `events` (both in the chart's ClusterRole).

## Configuration

The realization factor can be pinned with `impact.realizationFactor`, and the autoscaler of nodes outside Karpenter pools named with `impact.autoscalerHint` when detection cannot see it. `config.recommendations.karpenterEnabled` and `sources.settings["greenkube.nodepool"]` are ignored since 0.4 (the chart's notes name them).

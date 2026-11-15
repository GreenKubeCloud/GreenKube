# Karpenter and the Cluster Autoscaler (`greenkube.nodepool`)

> This page documents Karpenter and the Cluster Autoscaler. The file keeps its historical name (`nodepool.md`) so existing links work; the source id is `greenkube.nodepool` (v1 `source: karpenter`).

GreenKube reads the cluster's autoscalers for three purposes (roadmap G5): to recommend consolidating under-utilized Karpenter NodePools, to estimate how much released capacity really becomes removed nodes (the realization factor, from Karpenter or the Cluster Autoscaler), and to advise on Karpenter NodePool policies. The source is enabled automatically when the Karpenter CRDs exist; the Cluster Autoscaler is read for the realization factor only, and never produces recommendations.

Code: `src/greenkube/core/optimization/providers/karpenter.py`.

## NodePool consolidation (Karpenter)

For each Karpenter NodePool with at least two claimed nodes, GreenKube computes the time-weighted CPU request utilization of its nodes (requests over allocatable, from its own node and usage data). Below `NODE_UTILIZATION_THRESHOLD` (default 20%) it emits an `OVERPROVISIONED_NODE` recommendation (capability `node_pool`, target `nodepool/<name>`). The number of removable nodes keeps `ceil(nodes x utilization)` nodes, with utilization the larger of the CPU and memory request utilization, and the projection prices the cheapest removable nodes (node-hours x node price, realization factor 1, because the consolidation is the action itself). This is GreenKube's heuristic over Karpenter's state, not Karpenter's own advice. It is reviewed by a person: there is no NodePool pull request yet (roadmap H7).

## Realization context (not recommendations)

The controller reads, read-only (Cluster Autoscaler and Karpenter):

- the Cluster Autoscaler status ConfigMap `kube-system/cluster-autoscaler-status`;
- Karpenter NodePools (consolidation policy, disruption budgets) and NodeClaims (claim → pool);
- Karpenter events `DisruptionBlocked` (a blocker: the pool's factor is halved, with the event in the reasons) and `Unconsolidatable` (reported in the reasons, factor unchanged) on NodePools, NodeClaims and Nodes;
- `do-not-disrupt` / `safe-to-evict` annotations and blocking PodDisruptionBudgets.

These feed the [RealizationContext](../methodology/impact-model.md#realization-factor). Events that cannot be read (RBAC) are reported as "Karpenter events could not be read", never as "no blocker". When `PROMETHEUS_URL` is set, Karpenter's metrics are read too (no extra permission): `karpenter_voluntary_disruption_decisions_total` (consolidation decisions over 7 days, listed as evidence), `karpenter_voluntary_disruption_eligible_nodes` (no decision while nodes are eligible halves the factor of consolidating pools) and `karpenter_nodepools_cost_total` (cost increase per pool, recorded as `observed_cost`). Metrics that are not exported change nothing.

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

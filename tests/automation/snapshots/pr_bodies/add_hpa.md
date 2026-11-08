# greenkube: autoscale payments/api · −$300/yr · −12 kgCO₂e/yr

## Summary

GreenKube proposes to **autoscale** `payments/Deployment/api` (2-6 replicas @70% cpu), from recommendation [#61](https://greenkube.example.com/recommendations/61).

| Projected saving (realization factor 0.80) | per month | per year |
| --- | ---: | ---: |
| Cost (USD) | $25.00 | $300.00 |
| CO₂e | 1.0 kgCO₂e | 12.0 kgCO₂e |

_Projected by the Impact Model; measured savings are reported on this pull request after the rollout._

## Changes

- Add a HorizontalPodAutoscaler for `Deployment/api`: 2–6 replicas, target CPU utilization 70%.

## Why

**[#61](https://greenkube.example.com/recommendations/61) · autoscaling** — `payments/Deployment/api` · container `api`
- Finding: Memory p99 is far below the request.
- Consensus: 1 source (greenkube); proposed memory 512Mi
- Evidence: 7 days (2026-10-14 12:00 UTC → 2026-10-21 12:00 UTC), 2016 samples, 98% coverage
- CPU usage: p95 120m · p99 150m · max 180m
- Memory usage: p95 300Mi · p99 340Mi · max 380Mi
- Stability: 0 restarts, 0 OOM events in the window
- Confidence: 92%
- Chart and full evidence: [GreenKube](https://greenkube.example.com/recommendations/61)

## Risk & what to watch

Risk: **medium**.

Risk factors:
- p95 usage remains below target

Autoscalers and disruption budgets (HPA, VPA, PDB):
- This plan adds a HorizontalPodAutoscaler (2–6 replicas). Its CPU target is a share of the container requests, so later request changes move the scaling point; no other autoscaler (HPA, KEDA, a VPA in an active mode) should control the replicas, and a PodDisruptionBudget must still allow evictions at the minimum replica count.
- The policy result carries no HPA, VPA or PDB evidence for this workload; check the autoscalers and disruption budgets that target it before merging.

Watch these for 24h after the rollout:
- Review if CPU p95 exceeds 220m.
- container restarts and `OOMKilled` events
- CPU throttling of the changed containers
- readiness, error rate and latency of the service

## Assumptions

- Unit prices:
  - $0.0316/core-hour, $0.0042/GiB-hour, source `pricing_catalog` (aws/eu-west-3/m5.large)
- Observed cost sources: `opencost`
- Carbon intensity: 56 gCO₂e/kWh (source: stored grid intensity of the observed samples, node-profile intensity where a sample has none); power model profile
- PUE: 1.14
- Realization factor: 0.80 (`realization_context`)
  - karpenter consolidation is enabled on pool default
- Carbon price: $100.00/tCO₂e (ranking only)
- Impact Model version: 1.1.0

## Rollback

- Revert this pull request; the GitOps controller syncs the previous values.
- GreenKube cannot build an automated rollback for this layout or action kind; revert this pull request manually.

## Verification plan

1. After the merge, GreenKube waits for the Argo CD Application `argocd/payments-prod` to sync the merge commit, then for the rollout to complete.
2. Measurement starts after a 1h warm-up and lasts 7 days; it is compared with a baseline of 7 days that ends at the merge, matched hour of the week and normalized for load, with a 90% confidence interval.
3. The outcome is `realized`, `not_realized`, `inconclusive` or `contaminated`; health gates (restarts, OOM kills, throttling, readiness) can send the change to rollback review.

Results are posted to a GreenKube status comment on this pull request (updated at each step) and [the savings ledger](https://greenkube.example.com/ledger?change=pln-1).

<details><summary>Plan provenance</summary>

- Plan: `pln-1`
- Digest: `sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd`
- Provider: `github-main`
- Repository: `acme/gitops`
- Controller: `argocd/argocd/payments-prod`
- Renderer: `raw`
- Source path: `apps/payments`
- Base branch/revision: `main` / `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`
- Plan branch: `greenkube/plan/0123456789abcdef0123456789abcdef`
- Render validation: `passed`
- Files: `apps/payments/deployment.yaml` (update)
- Observed controller revision: `aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`
- Policy: `allowed`, 0 warning(s)

</details>

<details><summary>Source diff</summary>

```diff
--- a/apps/payments/deployment.yaml
+++ b/apps/payments/deployment.yaml
-cpu: 500m
+cpu: 200m
```

</details>

<details><summary>Rendered diff</summary>

```diff
-        cpu: 500m
+        cpu: 200m
```

</details>

<!-- greenkube:v1 {"plan_id":"pln-1","recommendation_ids":[61],"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","change_set_id":null,"digest":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"} -->
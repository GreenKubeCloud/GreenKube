# greenkube: raise resources of payments/api (mem 1Gi→2Gi) · +$150/yr

## Summary

GreenKube proposes to **raise resources of** `payments/Deployment/api` (mem 1Gi→2Gi), from recommendation [#43](https://greenkube.example.com/recommendations/43).

| Projected cost (reliability fix) | per month | per year |
| --- | ---: | ---: |
| Cost (USD) | +$12.50 | +$150.00 |
| CO₂e | +333 gCO₂e | +4.0 kgCO₂e |

_Projected by the Impact Model; a request increase reserves more capacity, so it is reported as a cost, never as a saving._

**Reliability fix.** This pull request raises requests to reduce the risk of OOM kills, CPU contention and evictions; it is never approved automatically unless `automation.autoApprove.increases` allows it.
- `api`: 3 OOM kills, memory p99 980Mi · max 1100Mi for a 1Gi request in the observation window.

## Changes

| Container | Resource | Current | Proposed | Change |
| --- | --- | ---: | ---: | ---: |
| `api` | requests.memory | 1Gi | 2Gi | +1Gi (+100%) |
| `api` | limits.memory | 1.5Gi | 3Gi | +1.5Gi (+100%) |

## Why

**[#43](https://greenkube.example.com/recommendations/43) · memory_rightsizing** — `payments/Deployment/api` · container `api`
- Finding: Reliability: containers were OOM-killed.
- Consensus: 1 source (greenkube); proposed memory 2Gi
- Evidence: 7 days (2026-10-14 12:00 UTC → 2026-10-21 12:00 UTC), 2016 samples, 98% coverage
- CPU usage: p95 120m · p99 150m · max 180m
- Memory usage: p95 900Mi · p99 980Mi · max 1100Mi
- Stability: 0 restarts, 3 OOM events in the window
- Confidence: 92%
- Chart and full evidence: [GreenKube](https://greenkube.example.com/recommendations/43)

## Risk & what to watch

Risk: **low**.

Risk factors:
- oom_history

Headroom over observed usage:
- `api` memory: proposed 2Gi is 2.28× the p95 (900Mi) and 1.86× the max

Autoscalers and disruption budgets (HPA, VPA, PDB):
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
- This pull request changes container limits, which the recorded pre-apply baseline does not hold: GreenKube cannot build an automated rollback; revert this pull request manually.

## Verification plan

1. After the merge, GreenKube waits for the Argo CD Application `argocd/payments-prod` to sync the merge commit, then for the rollout to complete.
2. Measurement starts after a 1h warm-up and lasts 7 days; it is compared with a baseline of 7 days that ends at the merge, matched hour of the week and normalized for load, with a 90% confidence interval.
3. A reliability increase is judged on reliability: GreenKube compares the OOM-kill rate after the rollout with the rate observed before it, and reports the measured cost as a cost. Health gates (restarts, throttling, readiness, and OOM kills that do not drop) can send the change to rollback review.

Results are posted to a GreenKube status comment on this pull request (updated at each step) and [the savings ledger](https://greenkube.example.com/ledger?change=pln-inc).

<details><summary>Plan provenance</summary>

- Plan: `pln-inc`
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
-memory: 1Gi
+memory: 2Gi
```

</details>

<details><summary>Rendered diff</summary>

```diff
-        memory: 1Gi
+        memory: 2Gi
```

</details>

<!-- greenkube:v1 {"plan_id":"pln-inc","recommendation_ids":[43],"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","change_set_id":null,"digest":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"} -->
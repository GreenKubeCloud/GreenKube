# greenkube: raise resources of payments/api (cpu 500m→800m) · +$150/yr

## Summary

GreenKube proposes to **raise resources of** `payments/Deployment/api` (cpu 500m→800m), from recommendation [#44](https://greenkube.example.com/recommendations/44).

| Projected cost (reliability fix) | per month | per year |
| --- | ---: | ---: |
| Cost (USD) | +$12.50 | +$150.00 |
| CO₂e | +333 gCO₂e | +4.0 kgCO₂e |

_Projected by the Impact Model; a request increase reserves more capacity, so it is reported as a cost, never as a saving._

**Reliability fix.** This pull request raises requests to reduce the risk of OOM kills, CPU contention and evictions; it is never approved automatically unless `automation.autoApprove.increases` allows it.
- `api`: CPU p99 520m for a 500m request, CPU throttled in 12% of periods in the observation window.

## Changes

| Container | Resource | Current | Proposed | Change |
| --- | --- | ---: | ---: | ---: |
| `api` | requests.cpu | 500m | 800m | +300m (+60%) |
| `api` | limits.cpu | 1000m | removed | n/a |

## Why

**[#44](https://greenkube.example.com/recommendations/44) · cpu_rightsizing** — `payments/Deployment/api` · container `api`
- Finding: Reliability: p99 CPU is above the request.
- Consensus: 2 sources (greenkube, vpa); proposed cpu 800m
- Evidence: 7 days (2026-10-14 12:00 UTC → 2026-10-21 12:00 UTC), 2016 samples, 98% coverage
- CPU usage: p95 470m · p99 520m · max 610m
- Memory usage: p95 300Mi · p99 340Mi · max 380Mi
- Stability: 0 restarts, 0 OOM events in the window
- Confidence: 92%
- Chart and full evidence: [GreenKube](https://greenkube.example.com/recommendations/44)

## Risk & what to watch

Risk: **low**.

Risk factors:
- cpu_throttling

Headroom over observed usage:
- `api` cpu: proposed 800m is 1.70× the p95 (470m) and 1.31× the max

Autoscalers and disruption budgets (HPA, VPA, PDB):
- The policy result carries no HPA, VPA or PDB evidence for this workload; check the autoscalers and disruption budgets that target it before merging.

Watch these for 24h after the rollout:
- p95 CPU usage exceeds 110% of the proposed request
- any new restart after apply
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

Results are posted to a GreenKube status comment on this pull request (updated at each step) and [the savings ledger](https://greenkube.example.com/ledger?change=pln-lim).

<details><summary>Plan provenance</summary>

- Plan: `pln-lim`
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
+cpu: 800m
```

</details>

<details><summary>Rendered diff</summary>

```diff
-        cpu: 500m
+        cpu: 800m
```

</details>

<!-- greenkube:v1 {"plan_id":"pln-lim","recommendation_ids":[44],"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","change_set_id":null,"digest":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"} -->
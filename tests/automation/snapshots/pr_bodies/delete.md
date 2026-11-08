# greenkube: delete payments/api (Deployment) · −$96/yr · −3 kgCO₂e/yr

## Summary

GreenKube proposes to **delete** `payments/Deployment/api` (Deployment), from recommendation [#51](https://greenkube.example.com/recommendations/51).

| Projected saving (realization factor 0.80) | per month | per year |
| --- | ---: | ---: |
| Cost (USD) | $8.00 | $96.00 |
| CO₂e | 250 gCO₂e | 3.0 kgCO₂e |

_Projected by the Impact Model; measured savings are reported on this pull request after the rollout._

## Changes

- Delete `Deployment` `payments/api` from the repository.

## Why

**[#51](https://greenkube.example.com/recommendations/51) · zombie_cleanup** — `payments/Deployment/api` · container `api`
- Finding: Memory p99 is far below the request.
- Consensus: 1 source (greenkube)
- Evidence: not stored with this recommendation.
- Confidence: 92%
- Chart and full evidence: [GreenKube](https://greenkube.example.com/recommendations/51)

## Risk & what to watch

Risk: **low**.

Risk factors:
- p95 usage remains below target

Watch these for 24h after the rollout:
- container restarts and `OOMKilled` events
- CPU throttling of the changed containers
- readiness, error rate and latency of the service

## Assumptions

- Unit prices:
  - $0.0316/core-hour, $0.0042/GiB-hour, source `pricing_catalog` (aws/eu-west-3/m5.large)
- Observed cost sources: `opencost`
- Carbon intensity: 56 gCO₂e/kWh; power model profile
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

<!-- greenkube:v1 {"plan_id":"pln-1","recommendation_ids":[51],"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","change_set_id":null,"digest":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"} -->
# greenkube: rightsize payments/api · −$300/yr · −12 kgCO₂e/yr

## Summary

GreenKube proposes to **rightsize** `payments/Deployment/api` (cpu 500m→200m, mem 1Gi→512Mi), from recommendations [#41](https://greenkube.example.com/recommendations/41), [#42](https://greenkube.example.com/recommendations/42).

| Projected saving | per month | per year |
| --- | ---: | ---: |
| Cost (USD) | $25.00 | $300.00 |
| CO₂e | 1.0 kgCO₂e | 12.0 kgCO₂e |

_Projected by the recommendation sources; measured savings are reported on this pull request after the rollout._

> Recommendation evidence is unavailable for #42; review those records before merging.

## Changes

| Container | Resource | Current | Proposed | Change |
| --- | --- | ---: | ---: | ---: |
| `api` | requests.cpu | 500m | 200m | −300m (-60%) |
| `api` | requests.memory | 1Gi | 512Mi | −512Mi (-50%) |

## Why

**[#41](https://greenkube.example.com/recommendations/41) · cpu_rightsizing** — `payments/Deployment/api` · container `api`
- Finding: Observed p95 is well below the request.
- Consensus: 2 sources (greenkube, vpa); proposed cpu 200m
- Evidence: 7 days (2026-10-14 12:00 UTC → 2026-10-21 12:00 UTC), 2016 samples, 98% coverage
- CPU usage: p95 120m · p99 150m · max 180m
- Memory usage: p95 300Mi · p99 340Mi · max 380Mi
- Stability: 0 restarts, 0 OOM events in the window
- Confidence: 92%
- Chart and full evidence: [GreenKube](https://greenkube.example.com/recommendations/41)

## Risk & what to watch

Risk: **high**.

Risk factors:
- single replica

Headroom over observed usage:
- `api` cpu: proposed 200m is 1.67× the p95 (120m) and 1.11× the max
- `api` memory: proposed 512Mi is 1.71× the p95 (300Mi) and 1.35× the max

Watch these for 24h after the rollout:
- Review if CPU p95 exceeds 220m.
- container restarts and `OOMKilled` events
- CPU throttling of the changed containers
- readiness, error rate and latency of the service

## Assumptions

No Impact Model projection is stored with these recommendations; the savings are the sources' own estimates (method: impact_model/1.1.0/container_resources).

## Rollback

- Revert this pull request; the GitOps controller syncs the previous values.
- Or let GreenKube open the rollback pull request from [the change page](https://greenkube.example.com/changes/pln-1): when a health gate fails after the rollout, the recommendation enters rollback review and the inverse plan restores the recorded pre-apply values.

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

<!-- greenkube:v1 {"plan_id":"pln-1","recommendation_ids":[41,42],"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","change_set_id":null,"digest":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"} -->
# greenkube: revert rightsize payments/api (plan pln-1)

## Summary

GreenKube proposes to **revert plan `pln-1`** on `payments/Deployment/api` after its recommendations entered rollback review ([#41](https://greenkube.example.com/recommendations/41), [#42](https://greenkube.example.com/recommendations/42)).

## Changes

Restores the resources recorded on `payments/Deployment/api` before plan `pln-1` was applied:
- container `api`: reverts requests.cpu 200m, requests.memory 512Mi

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

**[#42](https://greenkube.example.com/recommendations/42) · cpu_rightsizing** — `payments/Deployment/api` · container `api`
- Finding: Observed p95 is well below the request.
- Consensus: 2 sources (greenkube, vpa); proposed cpu 200m
- Evidence: 7 days (2026-10-14 12:00 UTC → 2026-10-21 12:00 UTC), 2016 samples, 98% coverage
- CPU usage: p95 120m · p99 150m · max 180m
- Memory usage: p95 300Mi · p99 340Mi · max 380Mi
- Stability: 0 restarts, 0 OOM events in the window
- Confidence: 92%
- Chart and full evidence: [GreenKube](https://greenkube.example.com/recommendations/42)

## Risk & what to watch

Risk: **low**.

Risk factors:
- p95 usage remains below target

Watch these for 24h after the rollout:
- Review if CPU p95 exceeds 220m.
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

This pull request is itself the rollback of plan `pln-1`.
To undo it, revert this pull request.

## Verification plan

1. After the merge, GreenKube waits for the Argo CD Application `argocd/payments-prod` to sync the merge commit, then for the rollout to complete.
2. The rollback is confirmed when the workload runs the restored values again.

Results are posted to a GreenKube status comment on this pull request (updated at each step) and [the savings ledger](https://greenkube.example.com/ledger?change=rbk-1).

<details><summary>Plan provenance</summary>

- Plan: `rbk-1`
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

<!-- greenkube:v1 {"plan_id":"rbk-1","recommendation_ids":[41,42],"revision":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","change_set_id":null,"digest":"sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","rollback_of_plan_id":"pln-1"} -->
# GA qualification and GitOps runbook

This document is the release acceptance record for the optimization engine and GitOps automation. It distinguishes implemented behavior from remaining GA gates; the full target architecture and supported matrix are specified in the [GitOps automation specification](specs/gitops-automation.md).

## Current qualification status (2026-10-23)

- Built `greenkube/greenkube:review-20261023-r14` for `linux/arm64` and packaged `greenkube-0.3.0.tgz`. The r14 image is running in the isolated `greenkube-review` namespace at Helm revision 23; the original `K8s/` checkout was not modified.
- Full Python tests passed against SQLite/PostgreSQL 18 (the SQLite-only connection-serialization test is skipped in PostgreSQL mode and passes separately). Pyrefly reports zero errors; Ruff lint/format pass. Frontend tests/build pass (**217 tests**), Helm unit tests/lint pass (**244 tests**), and PostgreSQL migration/repository tests pass. The production frontend build retains its existing large-chunk warning.
- Live discovery reports Argo CD and Flux capabilities available, with six resolved fixture bindings and ten unrelated blocked bindings. The standard collector persisted per-container samples for both containers in the new raw fixture.
- The r14 API fetched and safely extracted the disposable Gitea repository archive at full commit `69e710fe8efa4eabf85e89321c50128767c7fc1b`: 46,267 compressed bytes, 183,388 extracted bytes, and 43 files. The snapshot identifies itself as an archive and remains within configured limits.
- One raw plan grouped four VPA recommendations across `web` and `telemetry-sidecar`, passed policy/render validation, and produced one Gitea PR (#6). A repeated `/apply-pr` request reused that open PR; Gitea still had one open PR. The PR was merged, Argo CD reached `Synced/Healthy` at merge commit `8a17624c5bd3fd35fb0a25af12ab4418b56c480c`, and the plan/action records reached `applied`. Both containers changed from 100m CPU / 128Mi memory to 11m / 50Mi.
- Post-apply verification completed for all four recommendations as `inconclusive` with reason `insufficient_trusted_savings_samples (0 < 2)`. Five post-apply container-metric samples per container show CPU at or below 1m, web memory around 5–6Mi, and sidecar memory below 1Mi; the direct Metrics API also returned 0m/5Mi for the current pod. The records correctly keep measured cost/carbon savings unset and `savings_realized=false`, because container cost/carbon is allocated from pod-level totals and all savings samples are estimated.
- Minikube does not enforce NetworkPolicy. A direct check confirmed an in-cluster renderer Pod could connect to the QA Gitea Pod despite its egress-deny policy. The optional per-Pod firewall init container was also tested; the Minikube runtime denied `NET_ADMIN`, so it could not install its rules. `rendererSandbox.enabled` remains false in the review Helm values.
- To safely test the live API/worker path without changing the cluster CNI, a temporary r8 renderer ran inside the Minikube node’s Docker `--internal` network under dedicated UID 20002, with read-only root, 768 MiB memory, one CPU, 32 PIDs, and no mounts. GreenKube in Minikube reached its private bridge IP; renderer connection attempts to the QA Gitea Pod, Kubernetes API, and public IP were blocked. The container and network were removed and `GITOPS_RENDERER_URL` restored to empty afterward.
- The live Minikube API created a validated Argo Helm plan without a mapping annotation (unique `app.resources` inferred), opened Gitea PR #7, and a repeated `/apply-pr` reused that PR. After merge, Argo was `Synced/Healthy` at `1f79e1e9812356c0cd1556eaefd4fbbf956b3de3`; requests changed from 200m/256Mi to 15m/100Mi and the plan/recommendations reached `applied`.
- The live API grouped four VPA recommendations across `web` and `telemetry-sidecar` into one render-validated Argo Kustomize plan/PR (#8). Repeating `/apply-pr` reused the same PR. After merge, Argo was `Synced/Healthy` at `69e710fe8efa4eabf85e89321c50128767c7fc1b`; both containers changed from 100m/128Mi to 11m/50Mi and the plan/recommendations reached `applied`.
- Both new workloads persisted real per-container metrics through the standard pipeline. Their savings outcomes are `inconclusive`; ledger rows are `prorated`, not measured. Do not treat their nonzero projected/allocated values as trusted realized savings.
- A one-time Argo Helm mapping flow is available when automatic inference is ambiguous. The plan dialog offers only server-resolved Git values-file/container choices; the API re-resolves the binding and render-validates the selected path. The mapping is stored with that plan and does not alter the binding or Application.
- r13 recovered eight stale raw-plan PR attempts that lacked plan/provider/PR references by quarantining them as `error`; the four recommendations remain linked to the single merged PR #6. Recommendations 154/155 each link to merged PR #7, and 156–159 each link to merged PR #8. The GreenKube API and disposable Gitea both report zero open PRs.
- Two terminal failed operations remain in the review database as audit history for the raw-plan attempts that preceded successful attempt 3: one was rejected after its binding became stale, and one provider PR-creation attempt failed. They are not queued, running, retrying, or stuck; the successful attempt still produced exactly one merged PR (#6) for those four recommendations.
- The branch-cleanup pass selected eight plan-linked terminal branches, deleted all eight after provider confirmation that their PRs were terminal and no open PR used them, and recorded eight completion-ledger entries. Two old `greenkube/plan/*` refs remain because they have no plan/provider linkage; they were not deleted. No unrelated or open branch was touched.
- Final release check: Helm revision 23 is deployed; both application containers are Ready with zero restarts; `/api/v1/health` returns `{"status":"ok","version":"0.3.0"}`; authenticated `/prometheus/metrics` responds 200 with zero queued/running/stuck operations, plans, or webhook deliveries. SQLite `PRAGMA integrity_check` returns `ok`, and migrations are at version 37. No application error/traceback or disk-full log appeared after storage recovery; the remaining startup warnings are the expected static electricity/carbon and default-PUE fallbacks.
- A transient Minikube disk-full event occurred while loading successive review images. It was recovered by pruning inactive Docker builder cache and removing only unused r8–r12 **review** image tags from the Minikube node; host image copies, PVCs, and non-review images were preserved. The node now has approximately 426 MiB free (99% used), and DiskPressure is false.
- **Explicit support boundaries:** Minikube does not enforce NetworkPolicy and denies `NET_ADMIN`, so the in-cluster renderer remains disabled. Live Helm/Kustomize API-worker plans were validated using the node-local Docker `--internal` renderer described above. Remote/cross-repository Flux chart artifacts and Argo CD inline values fail closed; only rightsizing actions have GitOps planners. Provider snapshots use bounded GitHub, GitLab, and Gitea archives pinned to the resolved full commit SHA; unavailable archive capabilities fail closed without a mutable-ref or per-file fallback. Legacy single-provider Helm values are retired; upgrades must migrate to `automation.providers`, Secret references, and repository allowlists. These limitations are documented in the support matrix and configuration reference.
- The pre-existing `argocd/greenkube-gitops` Application remains `Degraded` because its required Secret is absent; it was not modified. All QA and GreenKube review Argo Applications and Flux Kustomizations/HelmReleases are healthy.

## Implemented GitOps plan path

1. Metrics and per-container evidence produce typed rightsizing actions; recommendations without a safe container target remain non-actionable.
2. Argo CD and Flux resources are read and reconciled into persisted workload bindings. Equal-priority source ambiguity is explicit; provider selection is constrained by repository allowlists.
3. A plan request supplies persisted recommendation IDs only. GreenKube derives the action and binding server-side, snapshots an immutable provider revision, and validates raw YAML/JSON, Kustomize overlay, or supported Helm-subset changes and their rendered diff.
4. Approval requires the exact preview digest. A durable lease-protected worker revalidates the source, creates one atomic multi-file commit on a deterministic branch, and adopts a matching open PR on retry.
5. GitHub, GitLab, and Gitea adapters use provider-native commits. Credentials are read from mounted Kubernetes Secret keys; no credential values are stored in provider configuration or exposed by the plan API.
6. The recommendations page previews and approves validated plans; the `apply-pr` compatibility API also uses this same plan path. The Automation page reports discovery coverage, lists safe source candidates for ambiguous/unresolved bindings, supports audited expiring operator-approved resolution, and shows safe pending/open PR metadata.

No PR path requires workload annotations or a global Git token. Arbitrary local Argo Helm charts may use the optional `greenkube.cloud/helm-values-mapping` Application annotation to map a specific container to a Git-managed values path; this is chart-schema metadata, not a PR trigger or authorization annotation. See [configuration.md](./configuration.md). `tests/automation/test_plan_worker.py` covers the composed plan-worker, PR merge, controller sync, and apply-detection loop with deterministic provider/controller adapters; `tests/integration/test_ga_acceptance.py` covers verified lifecycle, savings attribution, and rollback-review persistence.

## Historical QA run (2026-10-22) and remaining GA gate

### Prior r19 local validation

The earlier `./scripts/validate_ga.sh` run used the disposable PostgreSQL 16 test database and passed Ruff lint/format, Pyrefly (zero errors), **2,264 Python tests with no skips** and **84% coverage**, **194 frontend tests**, the production frontend build, and Helm lint. The chart passed its **215 Helm unit tests**.

### Live dev/prod and GitOps results

- Built and loaded ARM64 image `greenkube/greenkube:qa-20261022-r19`; chart `greenkube-0.3.0` is deployed as dev revision 19 and prod revision 20. Both profiles are Ready. Final Minikube container-storage headroom was approximately 2.8 GiB; no user PVCs or pre-existing images were removed. The test Gitea repository is isolated; the local [K8s checkout](../../K8s) and GitHub were not modified or pushed.
- Port-forward and browser checks passed for dev and prod: UI and health returned 200, anonymous protected API returned 401, authenticated API calls returned 200, and the Recommendations UI showed container-targeted actions. Prometheus, OpenCost, Boavizta, and Kubernetes reported reachable. Electricity Maps is unconfigured, so the app uses its documented static carbon-intensity fallback.
- QA Argo raw, Kustomize, and Helm Applications are `Synced/Healthy` and report the expected source types. Flux GitRepository, Kustomizations, and HelmRelease are Ready. The separate pre-existing `greenkube-gitops` Argo release remains Degraded because its required Secret is absent; it was not modified.
- Five plan previews passed render validation: raw manifests, Argo Kustomize, Argo Helm values, Flux Kustomize, and Flux HelmRelease. Each targeted `web`; Kustomize/Helm rendered diffs reported no unrelated changes and requests stayed within limits. For the live Flux HelmRelease test, the merged change reduced only `web` CPU request from 100m to 22m; `telemetry-sidecar` stayed at 25m CPU and 64Mi memory.
- Approved plan `pln-802a4401d50fc7f010752a93` created one local Gitea PR (#1). Repeating `/apply-pr` for the same recommendation returned that same open PR. It was merged at `f335d19054ad2345a8a03409aa65246cda77ec7f`; Flux applied that revision, and GreenKube recorded the PR as `synced` and the plan as `applied`. Flux HelmRelease sync now checks the referenced GitRepository artifact revision (the HelmRelease `lastAttemptedRevision` is only the chart version). A `sync_timeout` can recover on later confirmation; other sync failures remain terminal.
- With real Metrics API container samples, recommendations 169 (CPU) and 171 (memory) for the Argo Helm `web` container produced plan `pln-431f49775030384a4be78a02` and one local Gitea PR (#2). Retrying `/apply-pr` for recommendation 169 reused the same PR; both recommendation records share that PR and plan. After merge commit `9b9ba66e4c48cba3223e60ac273937466ec8a001`, Argo became `Synced/Healthy` and the plan became `applied`. The diff changed only `web` from 500m CPU / 256Mi memory to 10m / 16Mi; `telemetry-sidecar` stayed at 100m CPU / 64Mi.
- The Minikube `v1beta1.metrics.k8s.io` APIService had been routed to Prometheus Adapter, whose resource rules expected a container label that Prometheus did not expose. It now routes to the already-running standard Metrics Server, which returns 63 PodMetrics objects covering 82 containers. The GreenKube chart grants only read-only `get/list` on `pods.metrics.k8s.io`; no `nodes/proxy` permission was added. This APIService route change is currently a live Minikube setting, not persisted in the dirty K8s checkout.
- The scheduled r19 pipeline stored real per-container CPU/memory samples for both containers in all five QA workloads. Container-scoped verification now reads the separate container metrics store and never falls back to pod aggregates or sibling containers. For the Argo Helm `web` change, 13 pre-apply and 10 post-apply samples show CPU p95 remained 1m; memory p95 was 540,672 bytes before and about 530,637 bytes after, both well below the new requests. Recommendations 169/171 remain `applied` / `inconclusive`, with measured savings unset: container cost, energy, and carbon are still allocated from pod-level totals and flagged `is_estimated=true`. Their savings ledger rows are `prorated`, not `measured`.
- The earlier r15 verifier result for recommendation 126 used estimated pod aggregates. r19 reclassified it as `applied` / `inconclusive`, cleared measured savings, and superseded all 27 incorrect `measured` ledger rows. The 970 raw and 20 hourly rows tagged `qa-synthetic-integration-fixture` were removed; the ten unsubmitted fixture-derived container recommendations were marked stale and retained for audit.

### Remaining release gate: trusted per-container cost/carbon measurement

Real per-container CPU and memory are now available through PodMetrics, with no `nodes/proxy` access. The remaining GA gate is trustworthy per-container cost/carbon attribution: GreenKube currently apportions pod-level cost, energy, and emissions across containers, uses a fallback PUE/carbon intensity, and marks those values estimated. The verifier therefore correctly refuses to claim measured savings. Negative OpenCost inputs are flagged untrusted and cannot produce negative projected savings.

Before GA promotion, provide a trusted container-level cost/energy/carbon attribution source (and a configured carbon-intensity provider), then repeat the before/after verification. The APIService routing change should also be persisted through the K8s GitOps source of truth when that dirty checkout can be safely updated. The successful resource update and prorated ledger values are not measured savings.

## Release acceptance checklist

Run from the repository root:

```bash
./scripts/validate_ga.sh
```

The gate must be green for:

- Python formatting, linting, type checking, and integration acceptance tests.
- Full Python test suite with coverage threshold.
- Frontend tests and production build.
- Helm lint and chart unit tests (when Helm and the unittest plugin are installed).
- A clean working tree after validation.

For production promotion, additionally run the PostgreSQL contract tests with `GREENKUBE_TEST_POSTGRES_DSN`, render the production Helm profile, and execute the Minikube smoke test from a cluster with Prometheus available.

## Incident and rollback runbook

1. Stop promotion if the verifier reports `rollback_review`, `inconclusive`, or a failed health gate. Do not manually mark the recommendation `verified`.
2. Inspect the recommendation event trail and the pull request URL before changing the workload.
3. Revert the merged Git commit through the provider. The API has no separate revert action; keep the lifecycle event trail and do not report a recommendation as `verified` after a rollback.
4. Confirm that the savings API no longer attributes periods after rollback and that superseded ledger rows remain available for audit.
5. Re-run the acceptance test and the relevant provider, verifier, and database contract suites before reopening promotion.

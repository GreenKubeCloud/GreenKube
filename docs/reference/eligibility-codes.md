# Eligibility codes

Every recommendation returned by `GET /api/v1/recommendations`, `/recommendations/active`, `/recommendations/top` and `/recommendations/{id}` carries an `eligibility` block. It says whether GreenKube can open a pull request for it now and, when it cannot, why and what would make it eligible (roadmap F10, principle 6: fail closed, explain why).

```json
{
  "can_open_pr": false,
  "delivery_modes": ["manual"],
  "blockers": [
    {
      "code": "warming_up",
      "message": "Collecting evidence: 12/36 samples, 17% coverage (minimum 50%).",
      "fix": "Eligible in ~31h (around 2026-11-10T07:05+00:00); no action is needed."
    }
  ],
  "warnings": [],
  "eligible_at": "2026-11-10T07:05:00+00:00",
  "action_kind": "container_resources",
  "recommendation_ids": null,
  "evaluated_at": "2026-11-09T00:00:00+00:00"
}
```

| Field | Meaning |
|---|---|
| `can_open_pr` | `true` when there is no blocker: a plan can be previewed and a pull request opened. |
| `delivery_modes` | `direct` (GreenKube opens the pull request) when `can_open_pr`; `manual` (apply the change yourself, then mark it applied so it is measured) for every active recommendation. |
| `blockers` | Why a pull request cannot be opened, each with a `code`, a `message` and a `fix`. |
| `warnings` | Non-blocking findings with the same shape. |
| `eligible_at` | When a warming-up recommendation is expected to have enough evidence (F7), otherwise `null`. |
| `action_kind` | The change GreenKube would write (`container_resources`), or `null` when the kind is not supported yet. |
| `recommendation_ids` | The change set a plan must be created for (several containers of one workload are one pull request); `null` when not eligible. |

Eligibility depends on live state (GitOps bindings, freeze windows, open pull requests, the clock), so it is computed when the recommendation is read and never stored. The checks run in this order: lifecycle status, action kind and container target, automation configuration, the workload's GitOps binding, evidence warm-up, then the automation policy. The policy part uses the same `AutomationPolicyEvaluator` that guards plan creation (read-only), so a recommendation shown as eligible passes the same gates when its plan is prepared. A read that fails (bindings, open pull requests, the policy evaluation) blocks instead of being skipped; the plan itself can still fail on repository content (for example `needs_chart_mapping`).

## Blockers

### Lifecycle

| Code | Meaning | Fix |
|---|---|---|
| `not_active` | The recommendation is ignored, applied, stale, expired or being verified. | Un-ignore it, or wait for the next scan to refresh it. |
| `pull_request_open` | A pull request is already open for this recommendation (its status is `pr_open` until the merged change is observed in the cluster). | Review and merge or close it. |
| `pull_requests_unavailable` | The open pull requests could not be read, so a duplicate cannot be ruled out. Reported for active recommendations GreenKube could otherwise deliver (fail closed). | Check the database connection, then retry. |

### Action kind and target

| Code | Meaning | Fix |
|---|---|---|
| `action_kind_unsupported` | GreenKube cannot write this kind of change yet. The message names the planned kind: `hpa` (H3), `schedule` (H4), `node_pool` (H7) or `advisory`. | Apply the change manually, then mark it applied so it is measured. |
| `deletion_requires_review` | A deletion (`ZOMBIE_POD`, `IDLE_NAMESPACE`, `ORPHANED_PERSISTENT_VOLUME`, `ORPHANED_LOAD_BALANCER`) is never automated: GreenKube shows why the resource appears unused, it cannot prove that nothing depends on it. See [Why GreenKube thinks a resource is unused](../recommendation.md#why-greenkube-thinks-a-resource-is-unused). | Check the evidence, confirm with the owners, delete the resource from Git (or with `kubectl` outside GitOps) and mark the recommendation applied. Annotate the resource with `greenkube.io/ignore="true"` if it is still needed. |
| `action_unavailable` | A container rightsizing recommendation has no typed action: the workload owner or container is unknown, or the proposal does not lower the current request. | The next scan rebuilds the action once these are known; otherwise apply manually. |
| `requires_container_target` | The action does not target exactly one named container of the workload. | Rescan once container metrics are collected; GreenKube never patches a whole pod spec. |
| `no_effective_change` | The proposed request equals the current one after unit normalization (`1Gi` = `1024Mi`, `0.5` = `500m`), so a pull request would change nothing. Plan creation fails with the same code when the Git source already declares the proposal, and the plan worker fails the plan before any Git write when the cluster already runs it (the recommendation is then recorded as applied by apply detection). | No action is needed; the next scan resolves the recommendation. |

### Automation and GitOps binding

| Code | Meaning | Fix |
|---|---|---|
| `automation_not_configured` | No plan service is configured in this process. | Enable automation and configure a Git provider ([GitOps automation](../automation.md)). |
| `automation_disabled` | `automation.enabled` is false or `automation.mode` is `disabled`. | Enable automation. |
| `binding_unavailable` | The GitOps bindings could not be read. | Check the database and `GET /api/v1/gitops/status`. |
| `binding_missing` | Discovery has no source for this workload. | Make sure Argo CD or Flux manages it, then `POST /api/v1/gitops/refresh`. |
| `ambiguous_binding` | Several sources could manage the workload. | `POST /api/v1/gitops/bindings/{namespace}/{kind}/{name}/resolve`. |
| `binding_unresolved` | Discovery could not find the source. | Check the Application/Kustomization/HelmRelease is synced, refresh, or resolve manually. |
| `binding_blocked` | The source is unsupported or not allowed. | Read the reason in `GET /api/v1/gitops/bindings`; adjust the allowlist or source. |
| `binding_stale` | The binding expired (`automation.bindingTtl`). | `POST /api/v1/gitops/refresh`. |

### Evidence and policy

| Code | Meaning | Fix |
|---|---|---|
| `warming_up` | Fewer samples than `automation.policy.minSamples` or coverage below `minCoverage`. `eligible_at` estimates when both are met, counting one sample per query step from the end of the observation window (multi-replica workloads usually get there sooner). | Wait; no action is needed. |
| `policy_evidence_missing` | Evidence the policy needs is missing: usage evidence, a risk level or confidence, or live discovery evidence under `missingEvidence: fail`. | Rescan and refresh GitOps discovery. |
| `policy_evaluation_unavailable` | The automation policy evaluator failed for this workload (for example its plan-history read failed), so its live gates were not checked (fail closed). | Check the database connection, then retry. |
| `policy_invalid` | `GITOPS_POLICY_JSON` (`automation.policy`) does not parse. | Fix the Helm values. |
| `policy_risk_exceeded` | Risk above `automation.policy.maxRiskLevel`. | Apply manually after review, or raise the maximum. |
| `policy_confidence_too_low` | Confidence below `automation.policy.minConfidence`. | Wait for more evidence, or lower the minimum. |
| `change_freeze_active` | A freeze window is active or the workload carries a freeze label. | Wait for the window to end or remove the label. |
| `workload_pr_limit_exceeded` | The workload has `maxOpenPullRequestsPerWorkload` open pull requests. | Merge or close one. |
| `workload_cooldown_active` | The workload changed less than `cooldownMinutes` ago. | Wait for the cooldown. |
| `rollout_in_progress` | The workload is rolling out. | Wait for the rollout to complete. |
| `controller_unhealthy` | The owning Argo CD or Flux object is suspended or unhealthy. | Restore it to a healthy, syncing state. |
| `policy_interaction_conflict` | An HPA scales on the changed resource, a VPA writes requests (`Auto`, `Recreate`, `InPlace`, `InPlaceOrRecreate`) or a PodDisruptionBudget blocks the rollout. | Remove the conflicting writer or apply manually. |

## Warnings

| Code | Meaning | Fix |
|---|---|---|
| `singleton_workload` | One replica: the rollout restarts the only pod. | Merge in a maintenance window or add a replica first. |
| `memory_decrease_best_effort` | Memory decreases take effect when the pod restarts. | Expect the saving after the next rollout. |
| `reliability_change` | The change increases resources (reliability lane) and costs money. | Weigh the incident risk it avoids against its cost. |
| `consensus_contested` | Sources disagree on the change: their agreement is below 0.5 or they propose opposite directions. The message lists the reasons. A contested recommendation is never approved automatically. | Review the per-source spread (`consensus.spread`, evidence) before opening the pull request. |
| `policy_evidence_missing` | Live evidence is missing and `automation.policy.missingEvidence` is `warn`. | Refresh GitOps discovery. |
| `policy_interaction_conflict` | An HPA scales on another resource than the one changed. | Review the HPA after the change. |
| `workload_plan_already_exists` | Another plan for the workload is still active. Not a blocker: preparing a preview reuses or replaces an unapproved one; an approved plan makes the preview fail with this code. | Finish or cancel the approved plan. |

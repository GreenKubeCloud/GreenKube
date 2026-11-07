# Concepts

GreenKube is an optimization control loop for Kubernetes clusters managed with GitOps:

1. It **aggregates** recommendations from several sources.
2. It **decides** which ones are worth doing.
3. It **delivers** them as pull requests.
4. It **proves** that the change reached the cluster and what it saved.
5. It keeps a **ledger** of the outcome.

This page defines the vocabulary used across the documentation, the API and the UI. Items marked *(planned)* describe the direction of the project and are not available yet.

```mermaid
flowchart LR
  S[Sources] --> R[Recommendations]
  R --> P[Plan<br/>preview + digest]
  P --> PR[Pull request]
  PR --> M[Merge]
  M --> Y[Sync<br/>Argo CD / Flux]
  Y --> A[Apply detection]
  A --> V[Verification]
  V --> L[Savings ledger]
```

## The loop at a glance

| Stage | Question it answers | Where to read more |
|---|---|---|
| Aggregate | What do my tools recommend, for which workloads? | [Recommendation lifecycle](recommendation.md#sources-evidence-and-ranking) |
| Decide | Which changes are worth it, and how risky are they? | [Recommendation lifecycle](recommendation.md#sources-evidence-and-ranking) |
| Deliver | Where does this workload live in Git, and what exactly changes? | [GitOps automation](automation.md) |
| Prove | Did the merge reach the cluster? Is the new configuration live? What did it save? | [Recommendation lifecycle](recommendation.md#lifecycle-states) |
| Ledger | What have we saved so far, measured versus projected? | [Recommendation lifecycle](recommendation.md#realized-savings) |

## Sources

A **source** produces optimization advice. GreenKube currently ships three:

| Source | What it contributes | How it is enabled |
|---|---|---|
| `greenkube` | Native analyzers: CPU and memory rightsizing, zombie pods, autoscaling candidates, off-peak scaling, idle namespaces, carbon-aware scheduling, node utilization, orphaned PersistentVolumes and LoadBalancers | Always on |
| `vpa` | Container targets from VerticalPodAutoscalers in recommendation mode (`updateMode: Off`). This includes the VPAs that Goldilocks creates. | Enabled automatically when the VPA CRD exists |
| `karpenter` | NodePool consolidation opportunities, computed by GreenKube from live NodePool and NodeClaim state | Enabled automatically when the Karpenter CRDs exist |

Each recommendation records its `source`, a source-specific reference (`source_ref`, for example `namespace/vpa-name`), and the list of every source that reported the same issue (`sources`).

When several sources recommend the same change type for the same target, GreenKube currently keeps the recommendation from the highest-priority source (`RECOMMENDATION_SOURCE_PRIORITY`, default `vpa,karpenter,greenkube`).

*(Planned)*
- KRR, Kubecost, Polaris and cloud recommenders as additional sources.
- A public API so that any tool can push recommendations.
- **Consensus**: instead of picking one source by priority, GreenKube will combine their values and show how much they agree.

## Recommendation

A **recommendation** is one proposed change on one target. It contains:

- **Target**: namespace, workload owner (kind and name), and container when the change is container-specific. Node-level and cluster-level recommendations target a node, a PersistentVolume or a Service instead.
- **Type and capability**: for example `RIGHTSIZING_CPU` in the `cpu_rightsizing` capability.
- **Proposal**: the current and proposed values (for example CPU request 500m → 200m), or a schedule for off-peak scaling.
- **Evidence**:
  - observation window, sample count and coverage;
  - usage distribution (average, p50, p90, p95, p99, max);
  - restarts;
  - the method used to estimate savings;
  - rollback conditions.
- **Projected impact**: estimated annual cost and CO₂e savings.
- **Risk** (`low`, `medium` or `high`), with the factors that explain it.
- **Confidence** (0–1), with the factors that explain it.
- **Effort** (`low`, `medium` or `high`).
- **Ranking score**: a weighted combination of carbon impact, cost impact, confidence, risk, effort and actionability. Profiles change the weights: `balanced`, `cost_first`, `carbon_first`, `quick_wins`, `low_risk`.
- **Expiry**: a recommendation that is not acted upon expires after `RECOMMENDATION_TTL_DAYS` (14 by default).

### Recommendation statuses

| Status | Meaning |
|---|---|
| `active` | Current and actionable |
| `ignored` | Hidden by a user (can be restored) |
| `stale` | No longer produced by the latest scan |
| `expired` | Not acted on before its expiry |
| `pr_open` | A pull request implementing it is open |
| `applied` | The change is live in the cluster (also the state of a measured change whose saving was not realized or inconclusive) |
| `verifying` | The post-change observation window is running |
| `verified` | The measured saving was realized and no health check failed |
| `regressed` | Reserved for drift detection: a realized saving that no longer holds (nothing sets it yet) |
| `rollback_review` | A health check failed after the change |
| `reverted` | A rollback reached the cluster |
| `failed` | The pull request was closed without merge, or the change could not be completed |

Every transition is written to an audit trail: `GET /api/v1/recommendations/{id}/events`.

## Delivering a change

### Binding

GreenKube finds where a workload is defined in Git by reading Argo CD Applications and Flux Kustomizations/HelmReleases. It does not require workload annotations. The result is a **binding**, which records:

- the repository, ref and path;
- the controller (`argocd`, `flux_kustomization`, `flux_helm_release`);
- the renderer (`raw`, `kustomize`, `helm_values`, `helm_release`).

A binding is `resolved`, `ambiguous` (several candidates; an operator can pick one), `unresolved`, `blocked` or `stale`.

### Plan

A **plan** is a validated set of file edits that implements one or more recommendations on one binding:

1. GreenKube takes a snapshot of the repository at an exact commit.
2. It renders the change in an isolated sandbox for Kustomize and Helm.
3. It checks that only the intended fields change.
4. It returns a diff identified by a **digest**.

Approval binds to that digest, so the reviewed change is exactly the committed change.

Policy gates run before a plan is created and again before it is approved:
- risk and confidence thresholds;
- evidence coverage;
- open pull request budget;
- cooldown;
- change freeze;
- rollout state;
- HPA and PDB interactions.

Plan statuses:

| Phase | Statuses |
|---|---|
| Normal flow | `draft` → `validated` → `approved` → `applying` → `pr_open` → `merged` → `synced` → `applied` |
| Alternatives | `needs_mapping`, `ambiguous`, `rejected`, `stale`, `superseded`, `closed`, `sync_failed`, `failed` |

### Pull request

A durable, idempotent worker:
- creates a deterministic branch;
- makes one atomic commit;
- opens a pull request (GitHub, GitLab or Gitea) that carries the evidence, impact, risk, rendered diff and verification expectations.

Merge and close events arrive by signed webhook or by polling.

What can become a pull request today: decreases of container CPU and memory requests on Deployments, StatefulSets and DaemonSets. Other recommendation types are advisory for now.

*(Planned)* Increases, limits, replicas and HPAs, KEDA cron schedules, deletions of orphaned resources, remote Helm charts, Karpenter NodePools; patch-file and CI-driven delivery; a GitHub App.

## Proving the change

### Sync

After the merge, GreenKube watches the Argo CD or Flux object that owns the workload. It waits until the controller reports the merged revision and a healthy state. A plan that does not sync within the configured timeout becomes `sync_failed`.

### Apply detection

Before the pull request is opened, GreenKube freezes a **pre-apply baseline**: workload UID, generation and per-container requests.

A change counts as applied only when two conditions hold:
- the live workload moved past that baseline (new generation, or a recreated object observed at its current generation);
- its per-container requests match the proposed values, within `RECOMMENDATION_APPLY_TOLERANCE`.

### Verification

Each applied change gets **one fixed-horizon measurement**:

- **Windows:** the after-window starts when the rollout completes (every replica updated and available) and lasts `VERIFICATION_WINDOW_HOURS` (72 by default). The baseline window has the same length and ends when the pull request was merged (or the plan approved), so it never overlaps the change.
- **Health (per replica):** per-pod p95 usage stays below the applied per-pod request plus headroom; per-pod restart deltas stay within the limit; no OOM kill, readiness above the minimum and CPU throttling below the maximum (read from kube-state-metrics and cAdvisor through Prometheus). A signal that cannot be read is reported `not_available`, never counted as a pass.
- **Savings:** only rows with trusted cost provenance count. A change is realized when the measured saving clears a small noise floor. The projection is not a gate; how close the measurement came to it is reported.

The outcome is `verified` (realized), `rollback_review` (a health check failed), or `applied` with the saving `not_realized` or `inconclusive`. Request rightsizing changes what a workload *reserves*, not what it *uses*: its usage-based CO2e difference is reported but never claimed.

Every decision is stored as an immutable measurement. A `not_realized` or `inconclusive` change is never re-tested automatically; `POST /api/v1/recommendations/{id}/remeasure` starts a new measurement that supersedes the previous one. An operator can dismiss a false-positive `rollback_review` with a reason, which returns the change to `applied` and measures it again. The CPU and memory records of one change stay one attribution group through both actions, so together they never claim the workload's saving twice.

> Verification is in **beta**. The measurement method is being reworked to use counterfactual baselines, confidence intervals and per-change provenance.

### Rollback

A rollback is a pull request too. GreenKube previews it from the original plan, restores only the files that still match its output, and runs it through the same approval and worker path. The recommendation becomes `reverted` once the rollback is synced and the original requests are observed again.

## Savings and the ledger

GreenKube distinguishes several kinds of savings figures. They are never added together.

| Kind | Meaning | Status |
|---|---|---|
| **Projected** | Expected annual impact of a recommendation, from the evidence and the cost/carbon model | Available |
| **Prorated** | The projection accrued over time for a change that is applied but not yet verified | Available |
| **Measured** | The before/after difference observed by verification | Available (beta) |
| **Allocated vs billed** *(planned)* | A request cut lowers what a workload is *allocated* immediately. The cloud bill only drops when nodes are removed, so this will be measured at node-pool level and reported separately. | Planned |

Every attribution cycle writes ledger rows with their method (`prorated` or `measured`). A recommendation that enters `rollback_review` has its earlier rows flagged `superseded`; they are never deleted. The API and the dashboard report measured and prorated totals separately.

*(Planned)* An append-only ledger linking each entry to its pull request, merge commit, synced revision and measurement method, with CSV export and export aligned with the FinOps FOCUS format.

## Where things run

| Process | Responsibilities |
|---|---|
| **Controller** (`greenkube start`) | Collection; carbon intensity; compression; GitOps discovery; sync observation; apply detection; verification; savings attribution. Uses leader election in production. |
| **API** (`greenkube-api`) | REST API and dashboard; recommendation scans; plan creation and approval; webhooks. In standalone mode it also runs the automation worker inline. |
| **Automation worker** (optional, production) | Executes approved plans, polls pull requests, cleans up branches |
| **Renderer sandbox** | Renders Kustomize and Helm snapshots with no access to the database, provider tokens or service-account token |

See [Architecture](architecture.md) for the detailed data flow.

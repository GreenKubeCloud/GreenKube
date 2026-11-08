# Savings measurement — methodology

**Strategy:** `request_rightsizing` version `1.0` (`greenkube.verify.strategy.RequestRightsizingStrategy`) · **Code:** `src/greenkube/verify/` · **Roadmap:** C1–C5, C9

GreenKube measures every applied change once, against a counterfactual: what the workload would have cost **without** the change. The method follows the spirit of IPMVP Option B (retrofit isolation with measurement of the affected system): a baseline period before the change, a reporting period after it, a model that adjusts the baseline to the reporting period's conditions, and an uncertainty statement.

This page is the reference for reviewing and auditing a measurement. When a rule below changes the numbers a measurement produces, the strategy version must change.

## Status

| Part | Status |
|---|---|
| Change records and baseline snapshots (C1) | **In use** for every change found by apply detection. |
| Measurement lifecycle and `/api/v2/changes` (C2) | **In use.** |
| Load drivers and hour-of-week counterfactual (C3, MVP) | **In use.** The OLS model (`metric ~ load + hour_of_week`) of C3 v2 is not implemented yet. |
| Confounders, bootstrap interval, outcome rule (C4) | **In use.** Pool-level confounders are not detected yet (see [Limitations](#limitations)). |
| One measurement per change, split across its recommendations (C5) | **In use.** |
| Append-only ledger entries, hash chain and restatement (C6) | Not yet. Measured savings reach the existing ledger through the recommendation (method `measured`). |
| Drift monitor (`realized → regressed`, C8) | The state exists; nothing sets it automatically yet. A newer change on the workload closes the older realized one. |

Changes applied before this release, and every change when `CHANGE_MEASUREMENT_ENABLED=false`, are still measured by the interim verifier described in [recommendation.md](../recommendation.md#lifecycle-states).

## Vocabulary

| Term | Meaning |
|---|---|
| **Change** | One pod-template revision rolled out on one workload: a GitOps plan merged and synced, or a change detected in the cluster. Several recommendations (the CPU and memory requests of one or more containers) can belong to one change. |
| **Baseline window** | The period before the change used to predict the counterfactual. |
| **Post-window** | The period after the change in which the saving is measured. |
| **Counterfactual** | The predicted value of a quantity in a post-window hour had the change not been made. |
| **Saving** | Counterfactual minus actual, per hour. Signed: a negative saving is a cost increase. |
| **Measured** | A saving from this method. It is never added to a **projected** saving (the [Impact Model](impact-model.md) estimate made before the change). |

## 1. Recording the change (C1)

Apply detection reads every Deployment, StatefulSet and DaemonSet on each lifecycle pass. For each workload it records a **spec observation** whenever its UID, generation, pod-template digest or HPA digest differs from the previous one. The pod-template digest is the SHA-256 of the canonical JSON of the pod template, with empty fields removed and the `pod-template-hash` label ignored.

When a recommendation is detected as applied (its rollout has completed and the live requests match the proposal), GreenKube records one immutable **change** per plan and workload revision:

| Field | Source |
|---|---|
| `recommendation_ids` | Every recommendation applied by that revision (members detected in a later pass are appended). |
| `plan_id`, `pr_url`, `merge_sha`, `gitops_revision`, `synced_at` | The GitOps plan and its pull request, when the change came through a pull request. |
| `workload_uid`, `generation_before/after`, `spec_digest_before/after` | The live workload and the last observation of a different revision (or the plan's pre-apply baseline for the generation). `details.spec_before_source` says which. |
| `rollout_started_at` | The first observation of the new revision, else the sync time, else the completion time (`rollout_started_source`). |
| `rollout_completed_at` | Every replica updated and available (`rollout_completed_source`: the Deployment `Progressing` condition, otherwise the detection time). |
| `baseline_window_end` | The PR merge, otherwise the plan approval, otherwise the rollout completion. |

At the same time GreenKube freezes a **baseline snapshot**: the workload's hourly observations over the baseline window, the load-driver series available for that window, the unit prices observed in it, the dominant carbon-intensity source and the Impact Model version. The snapshot carries a `content_sha256` over its content; `GET /api/v2/changes/{id}` reports whether it still verifies. Measurements always use the frozen snapshot, never a re-read of the baseline period.

## 2. Windows

| Window | Definition |
|---|---|
| Baseline | `[baseline_window_end − CHANGE_MEASUREMENT_PRE_WINDOW_DAYS, baseline_window_end)`, 7 days by default. It ends at the merge or approval, so it never contains the change. |
| Post | `[rollout_completed_at + CHANGE_MEASUREMENT_WARMUP_HOURS, + CHANGE_MEASUREMENT_POST_WINDOW_DAYS)`, 1 hour of warm-up and 7 days by default. |
| Interim health | `[post start, post start + VERIFICATION_WINDOW_HOURS)`, 72 hours by default. |

Seven days give every hour of the week one baseline observation. A shorter baseline leaves hours of the week unmatched, which lowers coverage (section 4).

## 3. Hourly observations

Rows of the workload (pod level, all containers) are reduced to one bucket per UTC clock hour:

- **Allocated cost** — only from rows whose cost provenance is trusted (`opencost`, `kubecost`, `pricing_catalog`; or, for legacy rows, a positive cost not flagged estimated). A sample time counts only when every replica row at that time is trusted. The rate divides by the covered time, so raw 5-minute rows and hourly rollups mix correctly.
- **CPU and RAM cost components** (`cost_cpu`, `cost_ram`) when every trusted row carries them.
- **Requested capacity** — requested CPU cores and memory GiB summed over replicas.
- **Replicas**, **CPU usage**, **energy**, **CO₂e** (only from rows with measured intensity and a profiled node), and the node **instance types** running the pods.

An hour counts for a quantity when at least half of it is covered by qualifying data.

## 4. Counterfactual (C3, MVP)

### Load driver

The counterfactual is adjusted by a **load driver**, the hourly amount of work the workload was asked to do. GreenKube takes the first candidate whose coverage is at least `CHANGE_MEASUREMENT_MIN_COVERAGE` in both windows and whose baseline is not all zero:

1. requests per second from the service mesh or ingress: Istio `istio_requests_total{reporter="destination"}`, Linkerd `response_total{direction="inbound"}`, then ingress-nginx `nginx_ingress_controller_requests` for a Service named after the workload;
2. the PromQL expression in the workload annotation `greenkube.io/load-metric` (`$namespace` and `$workload` are substituted);
3. the workload's CPU usage from GreenKube's metrics. It is the weakest driver (a CPU change can move it) and is flagged `weak`.

Prometheus drivers are read as hourly means (`avg_over_time((<expr>)[1h:5m])` with a one-hour step). Without Prometheus only the CPU-usage driver is available.

### Model

For each post-window hour `t` with hour of the week `h`:

```
counterfactual(t) = mean_baseline[h] × (load(t) / load_baseline[h]) ^ β
saving(t)         = counterfactual(t) − actual(t)
```

`β` is the **load elasticity** of the quantity, fitted on the baseline window as the least-squares slope of `log(quantity)` on `log(load)`, clamped to `[0, 1]`. It is 0 when fewer than 24 baseline hours pair a positive value with a positive load, or when the load hardly varies.

- An autoscaled workload, whose replicas follow its traffic, gets `β ≈ 1`: twice the traffic, twice the expected cost.
- A fixed-capacity workload, whose requests do not move with traffic, gets `β ≈ 0` and its counterfactual is not rescaled. Scaling it by the raw load ratio would credit any traffic growth as a saving (the defect behind BUG-27).

A post-window hour is **aligned** when it has a value and its hour of the week has a baseline mean (and, when `β > 0`, a load value). **Coverage** is the number of aligned hours divided by the number of hours in the post-window.

The fitted elasticities and the number of matched hours of the week are stored in the measurement as `counterfactual_params`.

## 5. Quantities

The `request_rightsizing` strategy measures, per hour:

| Quantity | Unit | Claimed | Role |
|---|---|---|---|
| `allocated_cost` | currency | yes | Decides the outcome. |
| `cost_cpu`, `cost_ram` | currency | no | Split the saving between the change's recommendations. |
| `core_hours`, `gib_hours` | core-hours, GiB-hours | no | Requested capacity freed. |
| `co2e_g` | gCO₂e | no | Reported, never claimed. |

The carbon model is usage-based: changing a request changes what a workload reserves, not what it uses, so a rightsizing does not claim a CO₂e saving. Allocated CO₂e (idle and embodied share) will be claimed once the Impact Model's allocation is wired into collection.

## 6. Uncertainty and outcome (C4)

### Interval

The hourly savings of the aligned post-window hours, in time order, are resampled with a **moving-block bootstrap**: blocks of 24 consecutive hours (one day, starting at any hour) are drawn with replacement until the series length is reached, and the mean is taken. `CHANGE_MEASUREMENT_BOOTSTRAP_SAMPLES` resamples (1000 by default) give the distribution of the mean; its 5th and 95th percentiles are the **90% confidence interval**. Day blocks keep the within-day correlation of the savings, which resampling single hours would ignore.

The generator is seeded from the change, the measurement sequence and the quantity, so the same evidence always produces the same interval.

### Outcome rule

The rule is applied once, at the end of the post-window (plus one hour of grace for late samples). There is no repeated testing.

| Condition (on `allocated_cost`) | Outcome |
|---|---|
| A severe confounder was found | `contaminated` |
| Coverage below `CHANGE_MEASUREMENT_MIN_COVERAGE` (0.8), or no trusted cost in a window | `inconclusive` |
| CI lower bound > 0 | `realized` |
| CI upper bound ≤ ε, with ε = `CHANGE_MEASUREMENT_EPSILON_RATIO` × mean counterfactual (2%) | `not_realized` |
| Otherwise | `inconclusive` |

Values are **signed and never floored**: a cost increase is reported as a negative saving with its interval.

**Projection accuracy** (measured annual saving ÷ projected annual saving) is reported for each claimed quantity. It is not a gate: a saving can be realized at 30% of its projection. It feeds the track record of the recommendation's source.

### Confounders

| Confounder | Severity |
|---|---|
| Another change on the same workload landed after the merge and before the end of the post-window | severe |
| The pod template changed again during the post-window (a spec digest other than this change's) | severe |
| The workload was deleted and recreated during the post-window | severe |
| Another change on the same workload landed inside the baseline window | moderate |
| The workload's HPA changed | moderate |
| Mean replicas deviate more than 25% from the replicas predicted by the same counterfactual | moderate |
| The unit price of a resource whose per-replica requests the change did not touch moved more than 5% | moderate |
| The node instance types running the workload changed (total-variation distance above 25% of pod-hours) | moderate |

**Confidence** starts at the coverage and loses 0.15 per moderate confounder, 0.05 per mild one and 0.10 when the counterfactual has no load driver or only the CPU-usage driver; it is 0 when the measurement is contaminated.

## 7. Health

The same per-replica health gates as the interim verifier apply to the change's recommendations, grouped by container: per-pod p95 usage under the applied request × headroom, restarts, OOM kills, readiness and CPU throttling. A signal that cannot be read is reported `not_available`, never counted as a pass.

They are evaluated twice:

- once the interim health window has elapsed, so a harmful change is flagged within `VERIFICATION_WINDOW_HOURS` instead of at the end of the post-window. A failure sends every awaiting recommendation of the change to `rollback_review`;
- at the decision, over the whole post-window. The gates are stored in the measurement.

A health failure does not change the measurement outcome: the saving is still evidence about the change. It changes what happens to the recommendations.

## 8. One measurement per change (C5)

A change is measured once, on its workload. The saving is split between its recommendations:

- **by cost component** when the workload's rows carry the CPU/RAM split: CPU recommendations share the `cost_cpu` saving and memory recommendations the `cost_ram` saving, each in proportion to the request reduction it made;
- otherwise **by projected share** of the `allocated_cost` saving (equal shares when nothing was projected).

Shares within a basis sum to one, so a change is never claimed twice. Each recommendation receives one immutable row in `savings_measurements` (`change_id`, `group_id` = the change measurement), with its share of the estimates and the change's outcome. Its status follows:

| Change outcome | Recommendation |
|---|---|
| `realized` | `verified`; the ledger accrues its measured share (method `measured`). |
| `not_realized` | `applied` with `verification_status=failed`; signed measured values are kept. |
| `inconclusive`, `contaminated` | `applied` with `verification_status=inconclusive` (reasons prefixed `contaminated:` for a contaminated change). |
| a health gate failed | `rollback_review`. |

A recommendation that joins a change after it was measured gets an `inconclusive` row: it was not part of the split.

## 9. Lifecycle (C2)

```
applied → measuring → realized | not_realized | inconclusive | contaminated
realized → regressed | closed
not_realized | inconclusive | contaminated → measuring   (explicit re-measurement only)
```

Each transition is a compare-and-swap on the stored state and version, written in the same transaction as its event (`change_recorded`, `measurement_started`, `health_checked`, `measurement_decided`, `remeasure_started`, `superseded`, `regressed`). A re-measurement (`POST /api/v1/recommendations/{id}/remeasure` on any of the change's recommendations) starts a new post-window at the next hour and a new measurement that supersedes the previous one; the baseline stays the frozen snapshot. A newer change on the same workload closes a realized change (`superseded`).

A decision persisted before a crash is replayed: a change measurement and each recommendation's row are unique per measurement cycle, so a second pass reuses them instead of measuring again.

## 10. Auditing a measurement

1. `GET /api/v2/changes/{id}` returns the change, its state history and its baseline snapshot metadata. `baseline.content_verified` must be `true`.
2. `GET /api/v2/changes/{id}/measurements` returns each decided measurement with its windows, load driver, `counterfactual_params`, per-quantity estimates and intervals, confounders, confidence, health gates, `allocations` and the per-recommendation rows.
3. `evidence_sha256` covers every decided field of the measurement and `baseline_sha256` the snapshot it used. Recomputing the counterfactual from the snapshot's `hours` and `load_series`, the post-window rows and the parameters above must give the same estimates; the bootstrap seed is derived from `change_id`, `sequence` and the quantity.
4. Database triggers reject `UPDATE` and `DELETE` on `change_events`, `change_members`, `baseline_snapshots`, `change_measurements`, `change_state_events` and `savings_measurements`.

## Limitations

- **Request changes only.** `request_rightsizing` is the only strategy. Replica, schedule, deletion and node-pool changes need their own strategies.
- **Allocated, not billed.** The measured saving is allocated cost. The bill only drops when nodes are removed; pool-level realization (C10) is not implemented.
- **Pool confounders.** Changes to other workloads on the same node pool are not detected as confounders.
- **One baseline week.** With the default windows each hour of the week has a single baseline value; the interval reflects that noise, but an unusual baseline week (a holiday, an incident) biases the counterfactual. Use a longer `CHANGE_MEASUREMENT_PRE_WINDOW_DAYS` for such workloads.
- **Load-driver heuristics.** The ingress-nginx driver assumes a Service named after the workload. Set `greenkube.io/load-metric` when that does not hold.
- **Spec observations depend on the lifecycle interval.** A spec change reverted between two detection passes is not seen.

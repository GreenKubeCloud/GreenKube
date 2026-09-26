# GreenKube Optimization Engine — Technical Specification

| | |
|---|---|
| **Status** | Phases 0–2 implemented; Phases 3–6 specified |
| **Scope of current iteration** | Phases 0–2 (unified engine, multi-source architecture, evidence model, ranking & risk) |
| **Deferred to later iterations** | Phase 3 apply detection & verification, Phase 4 PR bot, Phase 5 impact measurement, Phase 6 real VPA/Karpenter connectors |
| **Related documents** | [Recommendation lifecycle](../recommendation.md), [Automation plan](../automation-plan.md), [Architecture](../architecture.md), [API](../api.md), [Configuration](../configuration.md) |

---

## 1. Purpose

The recommendation component must evolve from a single native analyzer into an
**optimization engine** that:

1. **Aggregates recommendations from multiple sources** (GreenKube native, VPA in
   recommendation mode, Karpenter, later KEDA/Goldilocks) with explicit provenance
   and conflict resolution — a workload governed by VPA must not also receive a
   redundant (and likely inferior) native rightsizing recommendation.
2. **Treats every recommendation as a reviewable artifact**: observation window,
   current requests *and limits*, utilization distribution, proposed diff,
   confidence, expected savings, reliability risk, expiry date and rollback
   condition are attached to the recommendation itself — a reviewer must not have
   to reconstruct the analysis.
3. **Ranks recommendations** with a multi-criteria score (impact, cost, carbon,
   confidence, risk, effort, freshness, source authority) exposed with an
   explainable factor breakdown.
4. **Separates "apply succeeded" from "recommendation succeeded"**: after the
   change lands, the engine verifies both the **cost signal** and **workload
   health** over a defined window; if either leaves the expected bounds the
   recommendation stays open or enters rollback review.
5. **Provides a stable contract for the future PR bot** (`patch` payload, evidence
   and PR body contract) and for a **real savings ledger** (baselines, measured
   impact, verification outcomes).

The current iteration delivers points 1–3 end-to-end. Points 4–5 are fully
specified here so that data model, lifecycle and APIs do not need to change again
when they are implemented.

---

## 2. Non-goals (current iteration)

- No PR is opened against a GitOps repository. `docs/automation-plan.md` remains
  the implementation reference for that phase.
- VPA and Karpenter connectors ship as **real collectors behind disabled feature
  flags**; no cluster-wide enablement until Phase 6.
- No automatic apply detection or verification jobs run yet; their fields,
  statuses and interfaces are defined now.
- No breaking change to existing API responses; all additions are additive.
- No new storage backend; SQLite and PostgreSQL remain at parity.

---

## 3. Current state and pain points

### 3.1 Assets to preserve

| Asset | Location |
|---|---|
| 11 analyzers with tuned thresholds | `src/greenkube/core/recommender.py` |
| Persisted lifecycle (`active`/`applied`/`ignored`/`stale`) | `src/greenkube/models/metrics.py`, `recommendation_history` table |
| Repository abstraction (SQLite + PostgreSQL) | `src/greenkube/storage/base_repository.py` |
| Simple ranking by projected savings | `src/greenkube/core/recommendation_ranking.py` |
| Prorated savings ledger | `src/greenkube/core/savings_attributor.py`, migration `0008` |
| PR bot design | `docs/automation-plan.md` |
| Clean/hexagonal conventions | `core/factory.py`, `api/dependencies.py`, ABC repositories, collectors |

### 3.2 Blocking limitations

| # | Limitation | Evidence |
|---|---|---|
| 1 | Orchestration duplicated three times (API, startup scan, CLI), with drift risk | `api/routers/recommendations.py:127`, `api/startup.py:30`, `cli/recommend.py:61` |
| 2 | Monolithic 1,114-line recommender; analyzers not pluggable | `src/greenkube/core/recommender.py` |
| 3 | No source/provenance concept; no inter-source deduplication | no `source` field anywhere |
| 4 | No risk, confidence, effort, evidence or explainable ranking | `Recommendation` DTO |
| 5 | `RECOMMENDATION_APPLY_TOLERANCE` defined but unused; no apply detection | `config.py:218` |
| 6 | Savings ledger is 100% prorated projections; no baseline, no before/after measurement | `savings_attributor.py` |
| 7 | No limits, percentiles, window or expiry stored on recommendations | `recommendation_history` schema |
| 8 | `owner_kind` not persisted, blocking the future Git patcher | `docs/automation-plan.md` §3.1 |
| 9 | Statuses do not model PR/verification flows | `RecommendationStatus` enum |
| 10 | RBAC lacks VPA/Karpenter/apps read permissions; `k8s_client.py` exposes only CoreV1 + AutoscalingV2 | `helm-chart/templates/clusterrole.yaml`, `core/k8s_client.py` |

---

## 4. Design principles

1. **One orchestration path.** `OptimizationEngine` is the only component that
   builds a context, runs sources, deduplicates, scores and persists. API,
   startup and CLI are thin adapters.
2. **Source-agnostic core.** The engine knows `RecommendationSource` adapters,
   never vendor-specific logic. Connectors are infrastructure.
3. **Evidence-first.** A recommendation without evidence is not reviewable; every
   generated recommendation carries the evidence block defined in §6.7.
4. **Apply ≠ success.** `applied` records that a change landed; `verified` records
   that the outcome matched expectations. Savings are only "real" once verified.
5. **Additive and backward compatible.** Existing endpoints, DTO fields and
   Prometheus metric names keep working; new fields and labels are added.
6. **Deterministic.** Ranking, deduplication and evidence aggregation produce
   stable, testable output for identical inputs.
7. **Storage-agnostic.** The core never imports storage code; SQLite and
   PostgreSQL implementations stay at feature parity behind the same ABC.

---

## 5. Terminology

| Term | Definition |
|---|---|
| **Source** | Origin of a recommendation: `greenkube`, `vpa`, `karpenter`, … |
| **Capability** | What a recommendation changes: `cpu_rightsizing`, `memory_rightsizing`, `node_optimization`, `cleanup`, … Used for conflict resolution and ownership. |
| **Evidence** | Structured, self-contained justification block attached to a recommendation (window, percentiles, current/proposed resources, diff, confidence, risk, expiry, rollback conditions). |
| **Apply** | The change has landed in the cluster (manually, via PR merge, or via auto-detection). Status `applied`. |
| **Verification** | Post-apply observation window comparing cost signal and workload health against expected bounds. Outcome `verified` / `rollback_review` / `inconclusive`. |
| **Realization** | Conversion of expected savings into ledger rows. `prorated` (estimate-based) or `measured` (verification-based). |
| **Authoritative source** | The source that wins a capability conflict for a given target (see §6.6). |

---

## 6. Target architecture

### 6.1 Package layout

```text
src/greenkube/core/optimization/
├── __init__.py
├── engine.py                  # OptimizationEngine (single entry point)
├── context.py                 # OptimizationContext dataclass
├── context_builder.py         # metrics/nodes/HPA/PV/LB loading (deduplicated from API/startup/CLI)
├── registry.py                # source selection, feature flags, priority
├── dedup.py                   # merge + capability precedence + provenance
├── scoring.py                 # multi-criteria ranking, profiles
├── risks.py                   # risk level, confidence, effort
├── lifecycle.py               # state machine, transitions, event emission (Phase 3)
├── applied_detector.py        # K8s-based apply detection (Phase 3)
├── verifier.py                # cost + health verification, rollback review (Phase 3)
├── analyzers/
│   ├── base.py                # Analyzer ABC + shared statistical helpers
│   ├── zombie.py
│   ├── rightsizing_cpu.py
│   ├── rightsizing_memory.py
│   ├── autoscaling.py
│   ├── off_peak.py
│   ├── idle_namespace.py
│   ├── carbon_aware.py
│   ├── nodes.py
│   ├── orphaned_pv.py
│   └── orphaned_lb.py
└── providers/
    ├── base.py                # RecommendationSource ABC
    ├── native.py              # wraps analyzers
    ├── vpa.py                 # VerticalPodAutoscaler (recommendation mode)
    └── karpenter.py           # NodePool/NodeClaim (stub)
```

New shared storage mapper:

```text
src/greenkube/storage/recommendation_mapper.py   # single row → RecommendationRecord mapper
```

There is no compatibility façade: `core/recommender.py` has been removed and
all callers use the engine. The behavior suite exercises the native source
directly through a test helper.

### 6.2 Pipeline

```text
OptimizationEngine.refresh(namespace, persist=True)
  1. build_context()            → OptimizationContext
  2. run_sources(context)       → list[Recommendation]        (asyncio.gather, per-source isolation)
  3. normalize()                → capabilities, evidence defaults
  4. dedup_and_arbitrate()      → provenance, authoritative source, dropped duplicates
  5. assess_risk_and_confidence()
  6. score_and_rank(profile)    → ranking_score + ranking_factors
  7. persist()                  → upsert active + reconcile stale (+ events, Phase 3)
  8. emit_metrics()             → Prometheus gauges
```

`OptimizationEngine.generate(context)` exposes steps 1–6 without persistence for
the CLI (`greenkube recommend` remains a read-only reporting command).

Source failures are isolated: one failing connector logs a warning and yields an
empty list; the pipeline never crashes because VPA or Karpenter is unavailable.

### 6.3 `OptimizationContext`

```python
@dataclass
class OptimizationContext:
    config: Config
    namespace: str | None
    metrics: list[CombinedMetric]
    analysis_window_seconds: float | None
    window_start: datetime
    window_end: datetime
    node_infos: list[NodeInfo]
    hpa_targets: set[tuple[str, str, str]] | None
    persistent_volumes: list | None
    load_balancers: list | None
    live_workloads: dict[tuple[str, str, str], WorkloadSnapshot]  # Phase 3, empty before
```

`context_builder.py` centralizes everything currently duplicated in the three
call sites, including the Kubernetes namespace filter
(`_get_active_k8s_namespaces`) and OpenCost enrichment of PV/LB costs.

### 6.4 `RecommendationSource`

```python
class RecommendationSource(ABC):
    name: RecommendationSource
    priority: int
    capabilities: frozenset[RecommendationCapability]

    @abstractmethod
    async def is_available(self) -> bool: ...

    @abstractmethod
    async def collect(self, context: OptimizationContext) -> list[Recommendation]: ...
```

| Source | Availability check | Produces | Default |
|---|---|---|---|
| `native` | always | all native types | enabled |
| `vpa` | CRD `verticalpodautoscalers.autoscaling.k8s.io` discoverable | `RIGHTSIZING_CPU`, `RIGHTSIZING_MEMORY` (`source=VPA`) | disabled (`RECOMMENDATION_VPA_ENABLED=false`) |
| `karpenter` | CRD `nodepools.karpenter.sh` discoverable | `NODE_POOL` (Phase 6) | disabled |

**VPA mapping** (`providers/vpa.py`): list VerticalPodAutoscalers, keep those with
`spec.updateMode: "Off"` (recommendation-only), read
`status.recommendation.containerRecommendations[]` (`target.cpu`, `target.memory`,
`lowerBound`, `upperBound`, `uncappedTarget`), resolve the target from
`spec.targetRef` (`apiVersion`, `kind`, `name`) and emit recommendations with:

- `source = VPA`, `source_ref = "<namespace>/<vpa-name>"`,
- `owner_kind` / `owner_name` from `targetRef`,
- `capability` = `cpu_rightsizing` / `memory_rightsizing`,
- `evidence.proposed` = target values, `evidence.current` = live workload requests,
- savings estimated with the same annualization helpers as the native analyzer.

### 6.5 Analyzers

Each existing `_analyze_*` method becomes an `Analyzer` with unchanged thresholds
and formulas:

```python
class Analyzer(ABC):
    capability: RecommendationCapability

    @abstractmethod
    def analyze(self, context: OptimizationContext) -> list[Recommendation]: ...
```

Shared statistics move to `analyzers/base.py` (`_percentile`, `_usage_stats`,
`_latest_request_value`, `_balanced_rightsizing_target`,
`_annualized_window_total`, `_resource_savings_ratio`, minimum-threshold clamping,
target key derivation). Behavior is locked by the existing test suite.

### 6.6 Deduplication and precedence

**Identity for arbitration:** `(namespace, owner_kind, owner_name, capability)`,
falling back to `(namespace, pod_name, capability)`.

**Precedence:** ordered by `RECOMMENDATION_SOURCE_PRIORITY` (default
`vpa,karpenter,greenkube`). The highest-priority source owning a capability wins.

**VPA rule (decided):** if a VPA recommendation exists for a workload's
`cpu_rightsizing` or `memory_rightsizing`, the native recommendation for the same
target and capability is **dropped entirely** — not shown, not stored. This
guarantees no duplicate or inferior rightsizing advice when VPA is present.

**Provenance:** the surviving recommendation records every source that observed
the issue in `sources` (e.g. `["vpa", "greenkube"]`) so the UI and future PR body
can state "also identified by GreenKube native analysis".

**Second pass:** the existing deduplication on
`(scope, namespace, pod_name, target_node, type)` is preserved after arbitration.

### 6.7 Evidence model

Every recommendation carries a structured `RecommendationEvidence` block,
persisted as JSON (`evidence` column) and exposed by the API. This is the artifact
a reviewer reads; it must be sufficient without re-running any query.

```python
class UtilizationStats(BaseModel):
    avg: float
    p50: float
    p90: float
    p95: float
    p99: float
    max: float
    sample_count: int
    coverage_ratio: float          # observed samples / expected samples in window

class ResourceSnapshot(BaseModel):
    cpu_request_millicores: int | None
    cpu_limit_millicores: int | None
    memory_request_bytes: int | None
    memory_limit_bytes: int | None

class ProposedChange(BaseModel):
    resource: Literal["cpu", "memory", "node", "storage", "network"]
    current: float | None
    proposed: float | None
    change_ratio: float | None     # (current - proposed) / current

class RollbackCondition(BaseModel):
    metric: str                    # e.g. "cpu_p95_usage", "restart_rate", "oom_events", "cost_per_hour"
    comparator: Literal["gt", "gte", "lt", "lte", "outside_range"]
    threshold: float
    window_seconds: int
    action: Literal["review", "revert"]
    description: str

class RecommendationEvidence(BaseModel):
    observation_window_start: datetime
    observation_window_end: datetime
    observation_window_seconds: float
    sample_count: int
    coverage_ratio: float
    current: ResourceSnapshot
    proposed: ResourceSnapshot
    changes: list[ProposedChange]
    cpu_usage: UtilizationStats | None
    memory_usage: UtilizationStats | None
    restart_count: int | None
    oom_events: int | None
    cost_per_hour_before: float | None
    co2e_grams_per_hour_before: float | None
    proposed_patch: dict | None             # machine-readable action plan
    proposed_diff: str | None               # unified diff preview (filled by patcher, Phase 4)
    expected_savings_cost_annual: float | None
    expected_savings_co2e_grams_annual: float | None
    savings_method: str                     # e.g. "request_reduction_ratio", "opencost_observed", "flat_estimate"
    confidence: float                       # 0.0 – 1.0
    confidence_factors: dict[str, float]    # factor name → contribution
    risk_level: RiskLevel
    risk_factors: list[str]
    rollback_conditions: list[RollbackCondition]
    expires_at: datetime | None
    generated_by: str                       # source name
```

**Evidence completeness rules by recommendation type:**

| Field | Rightsizing | Cleanup (PV/LB/zombie/namespace) | Node | Carbon-aware | Off-peak |
|---|---|---|---|---|---|
| window + coverage | required | required | required | required | required |
| current/proposed requests | required | n/a | n/a | n/a | n/a |
| limits | required (when defined) | n/a | n/a | n/a | n/a |
| utilization percentiles | required | optional | required | required (intensity dist.) | required (hourly profile) |
| proposed diff | required | required (delete target) | required (drain/cordon) | optional | required (cron) |
| confidence | required | required | required | required | required |
| expected savings | required | required (cost) | required | required (CO2) | required |
| risk + rollback | required | required | required | low | required |
| expiry | required | required | required | required | required |

### 6.8 Risk, confidence, effort

**Risk level** (`low` | `medium` | `high`) with explicit factors, computed per
type. Examples:

| Situation | Level | Factor |
|---|---|---|
| CPU rightsizing where `p99` ≤ 80 % of proposed request | low | `sufficient_headroom` |
| CPU rightsizing where `max` ≥ proposed request | high | `observed_max_exceeds_proposal` |
| Memory rightsizing with any OOM/restart in window | high | `oom_history` |
| Off-peak scale-to-zero in a non-production namespace | medium | `availability_window` |
| Off-peak scale-to-zero in a production namespace | high | `production_availability` |
| Node drain / consolidation | high | `blast_radius_multi_workload` |
| Zombie pod deletion | low | `no_observed_usage` |
| Orphaned PV/LB deletion | low | `no_bound_consumer` |

**Confidence** (0–1) aggregates: sample count vs `RECOMMENDATION_MIN_SAMPLES`,
window coverage ratio, `is_estimated` flags on underlying metrics, source
authority (`vpa` receives a bonus for rightsizing), and metric variance.

**Effort** (`low` | `medium` | `high`): derived from actionability — `low` when a
`patch` is available and mechanical, `medium` for HPA/cron additions, `high` for
node operations.

### 6.9 Ranking and scoring

`scoring.py` replaces the current savings-only sort while keeping deterministic
tie-breaking.

```text
score = w_carbon   * norm(carbon_impact)
      + w_cost     * norm(cost_impact)
      + w_conf     * confidence
      + w_risk     * (1 - risk_weight)
      + w_effort   * (1 - effort_weight)
      + w_fresh    * freshness
      + w_source   * source_authority
      + w_action   * actionability        # 1 when a patch exists
```

- Impact normalization: `log1p` followed by min-max over the candidate set.
- Profiles: `balanced` (default), `carbon_first`, `cost_first`, `quick_wins`,
  `low_risk`.
- Weights are configurable (`RECOMMENDATION_RANKING_PROFILE`,
  `RECOMMENDATION_RANKING_WEIGHTS` as JSON override).
- Output: `ranking_score` (persisted) and `ranking_factors: dict[str, float]`
  (persisted, exposed) for "why this rank" explainability.
- Filters: `max_risk_level`, `min_confidence`, `source`, `capability`.

### 6.10 Lifecycle — apply success vs recommendation success

The current four statuses are extended. Two moments are explicitly distinct:

- **Apply succeeded** → `applied_at` is set, status `applied`.
- **Recommendation succeeded** → `verified_at` is set, status `verified`.

```text
                     ┌──────────┐
        ┌───────────►│ ignored  │◄──────────── restore
        │            └──────────┘
   ┌────┴────┐  no longer generated   ┌─────────┐
   │ active  ├───────────────────────►│  stale  │
   └─┬──┬────┘                        └─────────┘
     │  │ TTL elapsed                 ┌─────────┐
     │  └────────────────────────────►│ expired │
     │                                 └─────────┘
     │ PR opened (Phase 4)
     ▼
 ┌─────────┐  PR closed/failed   ┌────────┐
 │ pr_open ├────────────────────►│ failed │
 └────┬────┘                     └────────┘
      │ merged / detected
      ▼
 ┌─────────┐  verification window   ┌───────────┐  cost + health OK  ┌──────────┐
 │ applied ├───────────────────────►│ verifying ├───────────────────►│ verified │
 └────┬────┘                         └─────┬─────┘                   └──────────┘
      │ manual mark                       │ health out of bounds
      │                                   ▼
      │                            ┌──────────────────┐  rollback  ┌───────────┐
      │                            │ rollback_review  ├───────────►│ reverted  │
      │                            └──────────────────┘            └───────────┘
      │ change reverted/drifted
      ▼
 ┌───────────┐
 │ reverted  │
 └───────────┘
```

Status set (final target): `active`, `ignored`, `stale`, `expired`, `pr_open`,
`applied`, `verifying`, `verified`, `rollback_review`, `reverted`, `failed`.

Current iteration implements `active`, `ignored`, `stale` as today plus the new
`expired` status and the `expires_at` field. All other statuses are defined in
the enum and migration but only produced by Phase 3.

**Events:** every transition writes a row to `recommendation_events`
(`created`, `ignored`, `unignored`, `expired`, `pr_opened`, `pr_merged`,
`applied`, `verification_started`, `verified`, `rollback_review`, `reverted`,
`failed`) with actor, payload and timestamp — the audit trail for DevOps managers.

**Expiry:** `expires_at = generated_at + RECOMMENDATION_TTL_DAYS` (default 14).
An expired recommendation is hidden from active lists but retained for history;
a fresh scan may regenerate it.

### 6.11 Verification (Phase 3)

Triggered when a recommendation reaches `applied` (detection or manual).

1. **Freeze baseline** — the evidence block captured at apply time is stored as
   the baseline (`recommendation_baselines` or `evidence.baseline`).
2. **Observation window** — `VERIFICATION_WINDOW_HOURS` (default 72 h),
   `VERIFICATION_MIN_SAMPLES` (default 36 five-minute points).
3. **Cost gate** — measured cost/hour reduction must be ≥
   `VERIFICATION_MIN_SAVINGS_RATIO` (default 0.5) of the projected reduction.
4. **Carbon gate** — measured gCO2e/hour reduction must be consistent with the
   projection (same ratio, adjusted for grid intensity variation).
5. **Health gate** — all of:
   - restart delta ≤ `VERIFICATION_MAX_RESTART_DELTA` (default 0),
   - no new OOM kills,
   - `p95` usage ≤ proposed request × `VERIFICATION_USAGE_HEADROOM` (default 1.1),
   - readiness ratio ≥ `VERIFICATION_MIN_READINESS` (default 0.99),
   - CPU throttling ratio ≤ `VERIFICATION_MAX_THROTTLE_RATIO` (default 0.05).
6. **Outcomes:**
   - all gates pass → `verified`; ledger switches from prorated to measured.
   - any health gate fails → `rollback_review`; the recommendation stays open and
     the reverse `patch` is surfaced for review.
   - cost/carbon gate fails but health passes → status stays `applied` with
     `savings_realized=false`; the recommendation remains open for review.
   - insufficient samples → `inconclusive`; the window is extended once, then
     marked `inconclusive` and excluded from "verified savings".
7. **Rollback conditions** from the evidence block are evaluated continuously
   during the window; when triggered, the recommendation moves to
   `rollback_review` and `recommendation_events` records the condition.

### 6.12 Savings ledger evolution (Phase 5)

- `SavingsLedgerRecord` gains `measurement_method` (`prorated` | `measured`),
  `baseline_value`, `actual_value`, `confidence`.
- Attribution rules:
  - between `applied_at` and `verified_at`: prorated (as today);
  - after `verified_at`: measured actuals replace proration for the verified
    window and onward until drift;
  - on `rollback_review` / `reverted`: attribution stops and prior rows are
    flagged (`superseded=true`) rather than deleted.
- Realized Savings UI distinguishes **Projected**, **Measured (verified)** and
  **Prorated (pending verification)** totals — never mixes them silently.

### 6.13 Automation contract (Phase 4)

The engine produces a machine-readable `patch` payload per recommendation;
the patcher consumes it. This keeps Git logic out of the core.

```json
{
  "kind": "Deployment",
  "api_version": "apps/v1",
  "namespace": "prod",
  "name": "payments-api",
  "operations": [
    {
      "op": "set_container_resource",
      "container": "api",
      "resource": "cpu",
      "field": "requests",
      "value": "300m"
    }
  ]
}
```

**PR body contract** (rendered from the evidence block — the PR is a rendered
recommendation, not a separate artifact):

1. **Title** — `greenkube(optimization): rightsize Deployment/payments-api cpu 500m → 300m`
2. **Summary** — what changes, where, why.
3. **Impact** — annual/monthly cost savings and gCO2e savings, with the
   `savings_method` and assumptions stated explicitly.
4. **Risk & reliability** — `risk_level`, factors, blast radius, reversibility.
5. **Evidence** — observation window, sample coverage, utilization distribution
   table (avg/p50/p95/p99/max), current vs proposed requests and limits.
6. **Proposed diff** — unified diff.
7. **Verification plan** — what GreenKube will check after merge, the rollback
   conditions, and the expiry date.
8. **Provenance** — source (`vpa`/`karpenter`/`greenkube`), `source_ref`,
   recommendation ID and dashboard link.
9. **Reviewer checklist** — confirm non-production window, confirm HPA/PVC
   interactions, confirm rollback owner.

Merge detection (Phase 3/4) uses the **Kubernetes API only** for the first
iteration: when a live request decreases and a matching recommendation existed,
the recommendation transitions to `applied`. Webhook + polling via Git providers
is explicitly deferred; `application_method` already supports `webhook` and
`polling` so it can be added without schema change.

---

## 7. Data model and persistence

### 7.1 New/changed DTO fields

| Field | Type | Phase | Description |
|---|---|---|---|
| `source` | `RecommendationSource` | 1 | Origin of the recommendation (default `greenkube`) |
| `source_ref` | `str \| None` | 1 | Connector object reference (`ns/vpa-name`, NodePool name) |
| `sources` | `list[str]` | 1 | All sources that observed the issue (provenance) |
| `superseded_by` | `str \| None` | 1 | Source that won arbitration, when applicable |
| `capability` | `RecommendationCapability` | 1 | Change domain, used for arbitration |
| `owner_kind` / `owner_name` | `str \| None` | 1 | Workload owner, required by the Git patcher |
| `evidence` | `RecommendationEvidence` | 1 | Review-grade justification block (§6.7) |
| `risk_level` | `RiskLevel` | 2 | `low` / `medium` / `high` |
| `risk_factors` | `list[str]` | 2 | Explainable risk contributors |
| `confidence` | `float` | 2 | 0.0–1.0 |
| `effort` | `EffortLevel` | 2 | `low` / `medium` / `high` |
| `ranking_score` | `float \| None` | 2 | Persisted score for stable ordering |
| `ranking_factors` | `dict[str, float]` | 2 | Score breakdown |
| `patch` | `dict \| None` | 2 | Machine-readable action plan (same as `evidence.proposed_patch`) |
| `expires_at` | `datetime \| None` | 2 | TTL of the recommendation |
| `reversible` | `bool` | 2 | Whether the change has a clean reverse |
| `requires_restart` | `bool` | 2 | Whether applying restarts workloads |
| `applied_at` | existing | 3 | When the change landed |
| `application_method` | `str \| None` | 3 | `manual` / `detected` / `pr_merge` / `webhook` / `polling` |
| `verified_at` | `datetime \| None` | 3 | When the outcome was confirmed |
| `verification_status` | `str` | 3 | `pending` / `in_progress` / `passed` / `failed` / `inconclusive` |
| `verification_window_start/end` | `datetime \| None` | 3 | Observation bounds |
| `baseline` | `dict \| None` | 3 | Frozen pre-apply metrics |
| `measured_co2e_saved_grams` | `float \| None` | 3 | Measured carbon impact |
| `measured_cost_saved` | `float \| None` | 3 | Measured cost impact |
| `savings_realized` | `bool \| None` | 3 | Cost gate outcome |

Existing fields (`potential_savings_cost`, `potential_savings_co2e_grams`,
`current_*_request_*`, `recommended_*_request_*`, `cron_schedule`,
`target_node`, `priority`) are preserved unchanged.

### 7.2 New enums

```python
class RecommendationSource(str, Enum):
    GREENKUBE = "greenkube"
    VPA = "vpa"
    KARPENTER = "karpenter"

class RecommendationCapability(str, Enum):
    CPU_RIGHTSIZING = "cpu_rightsizing"
    MEMORY_RIGHTSIZING = "memory_rightsizing"
    AUTOSCALING = "autoscaling"
    OFF_PEAK = "off_peak"
    CARBON_AWARE = "carbon_aware"
    ZOMBIE_CLEANUP = "zombie_cleanup"
    NAMESPACE_CLEANUP = "namespace_cleanup"
    NODE_OPTIMIZATION = "node_optimization"
    STORAGE_CLEANUP = "storage_cleanup"
    LB_CLEANUP = "lb_cleanup"
    NODE_POOL = "node_pool"

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class EffortLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
```

`RecommendationStatus` is extended with `EXPIRED`, `PR_OPEN`, `VERIFYING`,
`VERIFIED`, `ROLLBACK_REVIEW`, `REVERTED` and `FAILED` (existing `ACTIVE`,
`APPLIED`, `IGNORED` and `STALE` are unchanged).

### 7.3 Migrations

| Migration | Content | Phase |
|---|---|---|
| `0010_recommendation_sources.sql` | `source`, `source_ref`, `sources`, `superseded_by`, `capability`, `owner_kind`, `owner_name`; index `(source, status)` | 1 |
| `0011_recommendation_evidence_ranking.sql` | `evidence` (JSON/JSONB), `risk_level`, `risk_factors`, `confidence`, `effort`, `ranking_score`, `ranking_factors`, `patch`, `expires_at`, `reversible`, `requires_restart`; indexes `(status, ranking_score)`, `(expires_at)` | 2 |
| `0012_recommendation_lifecycle_v2.sql` | new status values accepted, `application_method`, `verified_at`, `verification_status`, `verification_window_*`, `baseline`, `measured_*`, `savings_realized`; `recommendation_events` table | 3 |
| `0013_recommendation_pull_requests.sql` | PR tracking table (per `automation-plan.md` §3.2) | 4 |
| `0014_savings_ledger_measurement.sql` | `measurement_method`, `baseline_value`, `actual_value`, `confidence`, `superseded` | 5 |

Migrations are applied to **both** `sqlite/` and `postgres/` script directories
and are covered by `tests/integration/test_sqlite_migrations.py`.

JSON storage: `TEXT` on SQLite, `JSONB` on PostgreSQL. The mapper deserializes
into the Pydantic evidence model and tolerates `NULL` for old rows.

### 7.4 Repository changes

- New shared mapper `storage/recommendation_mapper.py` replaces the duplicated
  `_row_to_record` implementations in SQLite and PostgreSQL.
- `upsert_recommendations` refreshes the new columns for active records.
- `reconcile_active_recommendations` also marks active records past
  `expires_at` as `expired`.
- `get_top_recommendations` becomes a pre-filter (active, positive impact,
  optionally by risk/source) with final ordering performed by `scoring.py` in
  Python, preserving the bounded limit (1–50).

---

## 8. API changes

All additions are backward compatible.

| Method | Path | Change |
|---|---|---|
| `GET` | `/recommendations` | New query params `source`, `risk_level`, `capability`, `profile`; response includes new fields |
| `GET` | `/recommendations/active` | Same filters; expired records excluded |
| `GET` | `/recommendations/top` | New `profile` param; ranking factors in response |
| `GET` | `/recommendations/{id}` | **New** — full detail including `evidence`, `patch`, risk and verification fields |
| `GET` | `/recommendations/{id}/events` | **New (Phase 3)** — audit trail |
| `PATCH` | `/recommendations/{id}/apply` | Unchanged; accepts optional verification-related values (ignored until Phase 3) |
| `PATCH` | `/recommendations/{id}/ignore` | Unchanged |
| `DELETE` | `/recommendations/{id}/ignore` | Unchanged |
| `POST` | `/recommendations/{id}/apply-pr` | **Phase 4** — `dry_run` support |
| `GET` | `/recommendations/{id}/pull-requests` | **Phase 4** |
| `POST` | `/automation/webhooks/{provider}` | **Phase 4 (deferred)** — merge detection |

`TopRecommendation` gains `source`, `risk_level`, `confidence`, `effort`,
`ranking_score`, `ranking_factors`, `expires_at`.

---

## 9. Frontend changes

`frontend/src/routes/recommendations/+page.svelte`, `frontend/src/lib/api.js`:

- **Source badge** on each card (`GreenKube`, `VPA`, `Karpenter`).
- **Risk badge** with tooltip listing `risk_factors`; **confidence** indicator.
- **Evidence panel** (expandable): window, coverage, utilization distribution
  table, current vs proposed requests/limits, proposed diff, expiry, rollback
  conditions.
- **Ranking explanation**: `ranking_factors` breakdown and profile selector
  (`balanced`, `carbon_first`, `cost_first`, `quick_wins`, `low_risk`).
- **Filters**: source, risk level, capability.
- **Create PR button + preview modal** (Phase 4) rendered from the same evidence
  block as the PR body.
- **Verification state** on applied cards: `applied` vs `verified` vs
  `rollback_review`, with measured vs projected savings clearly separated.

---

## 10. Prometheus and Grafana

- Add a `source` label to `greenkube_recommendations_total` and the projected
  savings gauges; update `dashboards/greenkube-grafana.json` and
  `docs/prometheus-grafana.md` accordingly.
- New gauges (Phase 2/3):
  - `greenkube_recommendations_by_risk_total{risk,source,namespace}`,
  - `greenkube_recommendations_avg_confidence`,
  - `greenkube_recommendations_expiring_soon_total`,
  - `greenkube_recommendations_verified_total` (Phase 3),
  - `greenkube_recommendations_rollback_review_total` (Phase 3),
  - `greenkube_savings_measured_vs_projected_ratio` (Phase 5).

---

## 11. Configuration and Helm

New environment variables / Helm values under `config.recommendations`:

| Env var | Helm value | Default | Phase |
|---|---|---|---|
| `RECOMMENDATION_VPA_ENABLED` | `sources.vpa.enabled` | `false` | 1 |
| `RECOMMENDATION_KARPENTER_ENABLED` | `sources.karpenter.enabled` | `false` | 1 |
| `RECOMMENDATION_SOURCE_PRIORITY` | `sourcePriority` | `vpa,karpenter,greenkube` | 1 |
| `RECOMMENDATION_TTL_DAYS` | `ttlDays` | `14` | 2 |
| `RECOMMENDATION_RANKING_PROFILE` | `rankingProfile` | `balanced` | 2 |
| `RECOMMENDATION_RANKING_WEIGHTS` | `rankingWeights` | `{}` (profile defaults) | 2 |
| `RECOMMENDATION_MIN_SAMPLES` | `minSamples` | `36` | 2 |
| `VERIFICATION_WINDOW_HOURS` | `verification.windowHours` | `72` | 3 |
| `VERIFICATION_MIN_SAVINGS_RATIO` | `verification.minSavingsRatio` | `0.5` | 3 |
| `VERIFICATION_USAGE_HEADROOM` | `verification.usageHeadroom` | `1.1` | 3 |
| `VERIFICATION_MAX_RESTART_DELTA` | `verification.maxRestartDelta` | `0` | 3 |
| `VERIFICATION_MIN_READINESS` | `verification.minReadiness` | `0.99` | 3 |

RBAC additions (read-only, harmless when CRDs are absent):

- `autoscaling.k8s.io` → `verticalpodautoscalers` (`get`, `list`),
- `karpenter.sh` → `nodepools`, `nodeclaims` (`get`, `list`),
- `apps` → `deployments`, `statefulsets`, `daemonsets` (`get`, `list`)
  (required by apply detection in Phase 3; can be added now).

`k8s_client.py` gains `get_apps_v1_api()` and `get_custom_objects_api()`.

---

## 12. Testing strategy

Follow existing TDD + `pytest-asyncio` + `respx` patterns.

| Area | Tests |
|---|---|
| Engine/context | `tests/core/optimization/test_engine.py`, `test_context_builder.py` |
| Analyzer extraction | `tests/core/test_native_analyzers.py` and `tests/core/test_autoscaling_hpa.py` run against the native source through the synchronous test helper |
| Sources | `test_source_registry.py`, `test_vpa_provider.py` (mocked CustomObjectsApi), `test_karpenter_stub.py` |
| Dedup/precedence | `test_dedup_precedence.py`: VPA + native → VPA only; distinct capabilities coexist; provenance recorded |
| Evidence | `test_evidence_builder.py`: completeness per type, percentile math, coverage ratio, rollback conditions |
| Risk/confidence | `test_risks.py`: each heuristic, OOM/restart escalation, source authority bonus |
| Scoring | `test_scoring.py`: profile ordering, determinism, factor breakdown sums to score |
| Persistence | `tests/storage/test_recommendation_repository.py` (SQLite + PostgreSQL parity), JSON round-trip, expiry reconciliation |
| Migrations | `tests/integration/test_sqlite_migrations.py` extended for `0010`/`0011` |
| API | `tests/api/test_recommendations.py`: new filters, `/{id}` detail, profile ranking, additive compatibility |
| E2E | `tests/integration/test_recommendation_lifecycle_e2e.py` extended with evidence persistence and expiry |

Verification per phase: `pytest`, `ruff check`, `ruff format --check`, `pyrefly`
(pre-commit chain).

---

## 13. Delivery plan

### Phase 0 — Unified engine foundation (no functional change)

- Create `core/optimization/` package, `OptimizationContext`, context builder.
- Extract the 11 analyzers verbatim; no compatibility façade is retained.
- Single orchestration used by API, startup and CLI.
- Shared `recommendation_mapper.py`; both repositories refactored onto it.
- Exit criteria: full existing test suite green; API/CLI/startup behavior
  byte-for-byte equivalent (same recommendations, same savings).

### Phase 1 — Multi-source architecture and evidence model

- `RecommendationSource` ABC, registry, config, feature flags.
- `NativeSource`; `VpaSource` and `KarpenterSource` behind disabled flags.
- Deduplication/arbitration with VPA suppression rule and provenance.
- Evidence block generated by every analyzer (window, limits, percentiles,
  patch intent, savings method, rollback conditions, expiry).
- Migration `0010`; RBAC and `k8s_client` helpers.
- Exit criteria: VPA + native fixtures yield only VPA rightsizing; evidence
  completeness validated per type; SQLite/Postgres parity tests pass.

### Phase 2 — Ranking, risk and review surfaces

- `risks.py` (risk level, factors, confidence, effort).
- `scoring.py` with profiles and explainable factors.
- Migration `0011`; persistence of ranking/risk/expiry.
- API filters, `/{id}` detail, `profile` param; Prometheus `source` label.
- Frontend: badges, evidence panel, ranking explanation, filters.
- Exit criteria: deterministic ranking tests; API backward compatibility tests;
  frontend build and component tests pass.

### Phase 3 — Apply detection, verification and expiry (later)

- Lifecycle v2, `recommendation_events`, `AppliedDetector` (K8s API only),
  `verifier.py` (cost + health gates, rollback review), `expired` handling job.
- Migration `0012`; apps RBAC.
- Exit criteria: simulated apply/drift/OOM scenarios produce the expected
  transitions; savings attribution stops on rollback review.

### Phase 4 — PR bot (later)

- Implement `docs/automation-plan.md` with the PR body contract from §6.13.
- Migration `0013`, `PullRequestRepository`, `apply-pr` endpoint, frontend
  Create PR button. Webhooks explicitly deferred.

### Phase 5 — Measured savings ledger (later)

- Baselines, hybrid measurement, ledger `measurement_method`, Projected vs
  Measured UI separation, demo data update. Migration `0014`.

### Phase 6 — Real connectors (later)

- Enable `VpaSource` by default when detected; implement Karpenter NodePool
  consolidation recommendations.

---

## 14. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Refactor regression during analyzer extraction | Wrong recommendations in production | Phase 0 keeps formulas untouched; façade preserves legacy API; full suite + equivalence tests |
| SQLite/Postgres schema drift | Runtime errors on one backend | Shared mapper; parity tests; migrations shipped together |
| Evidence model becomes too heavy | Large DB rows, slow lists | Evidence stored per active record only (one row per identity); list endpoints return summary fields, full evidence only on `/{id}` |
| VPA mapping wrong for multi-container pods | Misleading recommendation | v1 handles per-container recommendations and aggregates conservatively; per-container targeting deferred with explicit risk note |
| Confidence/risk heuristics mistrusted | Users ignore scores | Factors are persisted and displayed; profiles and thresholds configurable |
| Verification false positives (traffic shift, seasonality) | Wrong rollback review | Minimum sample/window gates; baseline comparison at same hour-of-day for carbon; `inconclusive` rather than forced verdict |
| K8s-based apply detection needs `apps` RBAC | Detection silently disabled | `is_available()` logs a warning; detection degrades to manual apply; RBAC documented |
| Ranking non-determinism | Flaky tests, unstable UI order | Deterministic normalization + explicit tie-breakers |

---

## 15. Deferred decisions

- **Webhook + polling for PR merge** — architecture supports
  `application_method=webhook|polling`; implementation deferred per decision to
  use Kubernetes API detection first.
- **Multi-container rightsizing targeting** — v1 applies the same target to all
  containers; per-container annotation planned with the Git patcher.
- **Helm/Kustomize value-file patching** — Phase 4 follow-up per
  `automation-plan.md`.
- **Statistical significance for measured savings** — hybrid rule defined
  (minimum samples + ratio); full hypothesis testing deferred.
- **Recommendation priorities beyond `priority`** — superseded by
  `ranking_score`; the legacy field is kept for compatibility.

---

## Appendix A — Evidence example (CPU rightsizing)

```json
{
  "observation_window_start": "2026-09-19T00:00:00Z",
  "observation_window_end": "2026-09-26T00:00:00Z",
  "observation_window_seconds": 604800,
  "sample_count": 2016,
  "coverage_ratio": 0.98,
  "current": {
    "cpu_request_millicores": 500,
    "cpu_limit_millicores": 1000,
    "memory_request_bytes": 536870912,
    "memory_limit_bytes": 1073741824
  },
  "proposed": {
    "cpu_request_millicores": 300,
    "cpu_limit_millicores": 1000,
    "memory_request_bytes": 536870912,
    "memory_limit_bytes": 1073741824
  },
  "changes": [
    { "resource": "cpu", "current": 500, "proposed": 300, "change_ratio": 0.4 }
  ],
  "cpu_usage": {
    "avg": 120.4, "p50": 110.0, "p90": 180.0, "p95": 210.0, "p99": 260.0,
    "max": 310.0, "sample_count": 2016, "coverage_ratio": 0.98
  },
  "memory_usage": null,
  "restart_count": 0,
  "oom_events": 0,
  "cost_per_hour_before": 0.042,
  "co2e_grams_per_hour_before": 18.6,
  "proposed_patch": {
    "kind": "Deployment", "api_version": "apps/v1",
    "namespace": "prod", "name": "payments-api",
    "operations": [
      { "op": "set_container_resource", "container": "api",
        "resource": "cpu", "field": "requests", "value": "300m" }
    ]
  },
  "expected_savings_cost_annual": 73.58,
  "expected_savings_co2e_grams_annual": 32594.0,
  "savings_method": "request_reduction_ratio",
  "confidence": 0.86,
  "confidence_factors": { "samples": 0.9, "coverage": 0.98, "estimation_penalty": -0.05, "variance": -0.03 },
  "risk_level": "low",
  "risk_factors": ["sufficient_headroom"],
  "rollback_conditions": [
    { "metric": "cpu_p95_usage", "comparator": "gt", "threshold": 330.0,
      "window_seconds": 3600, "action": "review",
      "description": "p95 usage exceeds 110% of the proposed request" },
    { "metric": "restart_rate", "comparator": "gt", "threshold": 0.0,
      "window_seconds": 3600, "action": "review",
      "description": "any new restart after apply" }
  ],
  "expires_at": "2026-10-10T00:00:00Z",
  "generated_by": "greenkube"
}
```

## Appendix B — Source map

| Area | Main files (after implementation) |
|---|---|
| Engine & pipeline | `core/optimization/engine.py`, `context.py`, `context_builder.py` |
| Sources | `core/optimization/providers/`, `registry.py` |
| Analyzers | `core/optimization/analyzers/` |
| Dedup | `core/optimization/dedup.py` |
| Evidence & risk | `core/optimization/risks.py`, `models/evidence.py` |
| Ranking | `core/optimization/scoring.py`, `core/recommendation_ranking.py` |
| Lifecycle & verification | `core/optimization/lifecycle.py`, `applied_detector.py`, `verifier.py` |
| Persistence | `storage/recommendation_mapper.py`, `storage/{sqlite,postgres}/recommendation_repository.py` |
| Migrations | `core/migrations/scripts/{sqlite,postgres}/0010…0014` |
| API | `api/routers/recommendations.py`, `api/dependencies.py`, `api/schemas.py` |
| Prometheus | `api/metrics_endpoint.py` |
| Frontend | `frontend/src/routes/recommendations/+page.svelte`, `frontend/src/lib/api.js` |
| Automation (later) | `automation/` per `docs/automation-plan.md` |

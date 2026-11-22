# Recommendation sources (Source SDK)

GreenKube is a neutral aggregator: its own analyzers are one source of advice among others. Every source, native or third-party, implements the same contract and emits **signals** in one schema. This page explains the model, how sources run, and how to write and ship your own.

- Code: `src/greenkube/signals/` (model, SDK, adapters, runner, registry, contract checks).
- Example package: [`examples/greenkube-source-example`](../examples/greenkube-source-example).
- API: `GET /api/v1/sources`, `GET /api/v1/sources/{id}/runs`, `GET /api/v1/sources/{id}/signals`, and the push endpoints `POST /api/v1/signals` and `POST /api/v1/sources/krr/reports` ([API reference](api.md)).
- Connectors: [Vertical Pod Autoscaler](sources/vpa.md), [Goldilocks](sources/goldilocks.md), [Robusta KRR](sources/krr.md), [Push API](sources/push.md). Any other tool pushes its advice through the push API.
- Public signal schema: [`schemas/signal.v1alpha1.json`](../schemas/signal.v1alpha1.json) and [`schemas/signal-batch.v1alpha1.json`](../schemas/signal-batch.v1alpha1.json).
- UI: the **Sources** page lists every source with its status, last run, counts, track record and recent runs ([Web UI](ui.md#sources)).
- Configuration: `sources.pluginsEnabled`, `sources.settings` ([configuration](configuration.md#recommendation-sources-sources)).

## Concepts

| Term | Meaning |
|---|---|
| **Source** | A class that observes something (usage data, VPA objects, a vendor API, a report file) and returns signals. Identified by a stable `id`. |
| **Signal** | One source's proposal for one target and one capability (`SourceSignal`). |
| **Target** | The canonical, cluster-qualified object a signal is about (`TargetRef`), keyed `cluster/group.Kind/namespace/name[/container]`, with `_` for cluster-scoped objects. |
| **Capability** | The optimization domain: `container_requests`, `container_limits`, `replicas`, `horizontal_autoscaling`, `schedule`, `cleanup`, `node_pool`, `placement`, `hygiene`. |
| **Proposal** | The typed change: `container_resources`, `replicas`, `hpa`, `schedule`, `deletion`, `node_pool`, `advisory` (not PR-able). |
| **Source run** | One execution of one source in one scan, with its status, health and counts. |

Signals are not recommendations. The consensus engine ([Consensus across sources](recommendation.md#consensus-across-sources)) builds one recommendation per target, capability and finding kind from the signals of every source; a third-party source's signals become a recommendation only through it, under the source's own id. This is enforced, not a convention: the runner passes `SourceBatch.recommendations` to the recommendation pipeline only for built-in sources, and drops them for any plugin with a `recommendations_ignored` diagnostic on its run. The registry also rejects a plugin that sets `legacy_source` or subclasses a built-in adapter (`AnalyzerSource`, `RecommendationSourceAdapter`). Third-party advice can therefore never reach a pull request without going through consensus and risk review (FAIL CLOSED).

## The signal model

```python
SourceSignal(
    source_id="acme",                      # must be your source's id
    source_version="1.4.0",
    source_ref="shop/api-vpa",             # your own reference (object name, report id)
    engine_family=None,                    # set when several sources relay one engine (vpa for VPA and Goldilocks)
    target=TargetRef.for_kind(ctx.cluster_uid, "Deployment", "api", namespace="shop", container="web"),
    capability=Capability.CONTAINER_REQUESTS,
    kind="RIGHTSIZING",                    # finding kind; built-ins use the v1 type (RIGHTSIZING_CPU...)
    proposal=ContainerResourcesProposal(
        container="web",
        current=ResourceSpec(cpu_request_m=500),
        proposed=ResourceSpec(cpu_request_m=250),
    ),                                     # direction (decrease/increase/mixed) is derived
    algorithm=AlgorithmInfo(name="acme.p95", params={"percentile": 95}, window=ctx.window),
    evidence=SignalEvidence(summary="p95 is 120m over 14 days", details="...", data={"p95_m": 120}),
    observed_window=ctx.window,
    confidence=0.8,                        # your own confidence, 0..1, optional
    source_estimate={"monthly_cost": 12.0},# savings you claim; kept for comparison only
    raw={"object": "..."},                 # trimmed audit payload, at most 8 KiB
)
```

- `signal_id` is computed, never set: `sha256(source_id, target.key(), capability, kind, proposal identity)`. The proposal identity leaves out the observed state (`current`, and the `direction` derived from it) and free-text explanations (`reason`): they drift between scans without changing the advice. The same proposal keeps the same id across scans, and its stored row holds the latest state and reason; a changed proposed value gets a new id.
- GreenKube's Impact Model computes the savings shown to users. `source_estimate` is only kept to compare your claim with the model.
- Never invent values you do not have: optional fields stay `None` (an `HpaProposal` without bounds is valid).
- A pushed deletion (`DeletionProposal`) never opens a deletion pull request by itself: it only supports a deletion another source proposes on the same target. GreenKube's own orphaned PersistentVolume and LoadBalancer deletions can become a deletion pull request, never approved automatically ([Deleting orphaned resources](automation.md#deleting-orphaned-resources)).
- `engine_family` names the recommendation engine behind the advice when several sources relay the same one: consensus counts one vote per engine (`signal.engine`, the family or else the source id). Goldilocks and user VPAs both carry `engine_family: vpa`.

## Writing a source

```python
from typing import ClassVar, FrozenSet
from greenkube.signals import (
    Capability, CollectContext, Source, SourceBatch, SourceHealth, SourceLimits, SourceSettings, SourceTier,
)


class AcmeSettings(SourceSettings):          # validated from SOURCES_CONFIG["acme"]
    url: str = "http://acme.acme-system:8080"
    window_days: int = 7


class AcmeSource(Source):
    id: ClassVar[str] = "acme"                # lowercase, dotted segments; "greenkube." is reserved
    display_name: ClassVar[str] = "Acme recommender"
    description: ClassVar[str] = "Container requests from Acme."
    tier: ClassVar[SourceTier] = SourceTier.COMMERCIAL
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})
    version: ClassVar[str | None] = "1.4.0"
    settings_model: ClassVar[type[SourceSettings]] = AcmeSettings
    limits: ClassVar[SourceLimits] = SourceLimits(
        probe_timeout_seconds=10, collect_timeout_seconds=120, min_interval_seconds=600,
    )

    async def discover(self) -> SourceHealth:
        # Are the prerequisites there (a CRD, an endpoint)? Unavailable is not a failure.
        ...

    async def health(self) -> SourceHealth:
        # Can it serve now? DEGRADED still collects.
        ...

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        signals = [...]
        return SourceBatch(signals=signals, complete=True, diagnostics=[])
```

What a source receives:

- `self.settings`: its validated settings; `self.deps`: `config`, `cluster_uid` and a `clock`.
- `ctx.cluster_uid`, `ctx.namespace` (set for a namespaced scan: only emit signals in it), `ctx.window` (the scan's observation window), `ctx.deadline`.
- `ctx.inventory`: the workload targets GreenKube knows (one `TargetRef` per container). Use it to resolve pods to owners or to skip unknown workloads.
- `ctx.optimization`: GreenKube's internal analysis context. Built-in adapters use it; it is not a stable plugin API.

Rules:

- **Read only.** A source never writes to the cluster or to Git.
- **Partial results:** return `complete=False` when part of your input could not be read (a failed page, unreadable entries). GreenKube then keeps your previous advice instead of treating it as resolved.
- **Diagnostics** explain what you skipped (`Diagnostic(code="skipped_vpas", message="updateMode=Auto", count=12)`); they are stored with the run and shown by `GET /api/v1/sources/{id}/runs`.
- **Signals only.** Do not set `SourceBatch.recommendations` or `legacy_source`: they are reserved for built-in sources (see above).
- Raise on unexpected errors: the runner records the exception type (never its message, which may contain credentials) and the scan continues.

## How sources run

Scans run in the leader controller (`recommendation_scan`). For each enabled source, the `SourceRunner`:

1. skips it when its **circuit** is open (`failure_threshold` consecutive failures, default 3, until `circuit_cooldown_seconds`, default 15 minutes, passed) or when its **rate limit** (`min_interval_seconds`) has not elapsed; the run is recorded `skipped` and the last known advice is kept;
2. calls `discover()` then `health()` within `probe_timeout_seconds` (default 30 s); unavailable → `unavailable`;
3. calls `collect()` within `collect_timeout_seconds` (default 5 min; the engine caps it at 5 min);
4. validates the batch: signals with another `source_id` or an undeclared capability, and a plugin's `container_requests`/`container_limits` signals that propose no request or limit value (or a `RIGHTSIZING_CPU`/`RIGHTSIZING_MEMORY` signal without a `container_resources` proposal: diagnostic `missing_resource_value`), are dropped and make the batch incomplete; duplicates are merged; raw payloads over 8 KiB are replaced by a size marker, as are `evidence.data` and `source_estimate` over 8 KiB, and evidence `summary`/`details` are cut at 4096 characters (diagnostic `evidence_trimmed`); v1 recommendations of a plugin are ignored (diagnostic `recommendations_ignored`); signals beyond `max_signals` are dropped;
5. records the run in `source_runs` and upserts the signals in `source_signals` (one row per distinct signal, with first and last sighting; both kept 30 days).

Run statuses: `succeeded` (authoritative), `incomplete`, `failed`, `unavailable`, `skipped`. Only `succeeded` lets reconciliation resolve previous advice of that source. Circuit and rate-limit state come from the persisted runs, so they survive controller restarts and leader changes; when the controller cannot record runs, its own newer in-process runs take precedence. Native analyzers run one after the other, each within its own collection timeout; they are synchronous, so a timeout is recorded only once the analyzer returns.

`GET /api/v1/sources` derives a status per source: `healthy`, `degraded` (incomplete, skipped or degraded health), `failing` (failed, or explicitly `enabled: true` but unavailable), `unavailable`, `disabled`, `misconfigured` (invalid settings), `pending` (never ran) and `unregistered` (ran before, no longer installed).

## Built-in sources

| Id | Tier | Capability | v1 `source` | Notes |
|---|---|---|---|---|
| `greenkube.rightsizing_cpu`, `greenkube.rightsizing_memory` | native | container_requests | `greenkube` | Two-way rightsizing. |
| `greenkube.orphaned_pv`, `greenkube.orphaned_lb` | native | cleanup | `greenkube` | Review only. |
| `vpa` | oss | container_requests | `vpa` | VPAs in recommendation mode; signals of Goldilocks-created VPAs carry `source_id: goldilocks` ([VPA](sources/vpa.md), [Goldilocks](sources/goldilocks.md)). |
| `krr` | oss | container_requests | — | Robusta KRR reports pushed to `POST /api/v1/sources/krr/reports` ([details](sources/krr.md)). |
| `push` | custom | all | — | Batches pushed to `POST /api/v1/signals` by `push:<client>` ([details](sources/push.md)). |

`krr` and `push` emit signals only: they create no v1 recommendation and reach recommendations only through consensus, where GreenKube's measured value decides the size and they move the confidence. They read what was pushed over HTTP (the inbox, migration `0070`) into each scan, so pushed advice goes through the same runner, validation and storage as everything else. Built-in sources may emit signals under another source id declared in `also_emits` (`vpa` emits `goldilocks`; `push` emits `push:<client>`); plugins may not.

The native advisory analyzers (`greenkube.zombie`, `greenkube.autoscaling`, `greenkube.off_peak`, `greenkube.idle_namespace`, `greenkube.carbon_aware`, `greenkube.nodes`) and the NodePool heuristic (`greenkube.nodepool`) were removed in 0.4: their `sources.settings` entries are accepted and ignored, with one warning at start.

Native analyzers run through the analyzer adapter (`AnalyzerSource`), VPA through the v1 connector adapter (`RecommendationSourceAdapter`); their internals are unchanged and they keep emitting the same v1 recommendations. All native analyzers form the v1 source `greenkube` in `optimization_runs`: when one of them fails, `greenkube` is incomplete for that run and none of its recommendations go stale.

## Configuration

```yaml
sources:
  pluginsEnabled: true          # load greenkube.sources entry points
  settings:                     # SOURCES_CONFIG, keyed by source id
    greenkube.orphaned_lb: { enabled: false }
    vpa: { enabled: auto }      # auto: run when the VPA CRD exists
    acme: { enabled: true, url: "http://acme.acme-system:8080" }
```

`enabled` precedence: `sources.settings.<id>.enabled`, then `config.recommendations.vpaEnabled` for `vpa`, then the source's `default_enabled` (`auto`). Settings are validated by the source's `settings_model`; an invalid entry disables that source only and is reported. Disabling a native analyzer (`greenkube.orphaned_lb: {enabled: false}`) stops its advice: its active recommendations are no longer produced and are marked stale on the next complete scan, like advice whose condition went away. The ConfigMap holds these settings: never put credentials in them. Read secrets from environment variables or mounted files in your source.

## Packaging a third-party source

Declare an entry point in the `greenkube.sources` group; it resolves to a `Source` subclass or to a callable returning several:

```toml
[project.entry-points."greenkube.sources"]
"acme" = "acme_greenkube:AcmeSource"
```

Install the package into the GreenKube image (`pip install` in a derived image). At the next start the source appears in `GET /api/v1/sources` with `origin: plugin`. A plugin that fails to import, uses a reserved id or collides with another id is listed under `problems` and does not affect the other sources. Set `sources.pluginsEnabled: false` to load no plugin at all.

## Testing a source

`greenkube.signals.testing.check_source_contract(source, ctx, empty_ctx)` runs the contract every built-in source passes:

- valid metadata, settings with defaults for every field;
- `discover()`/`health()` return a `SourceHealth` within the probe timeout;
- `collect()` returns a `SourceBatch` within the collect timeout, with signals that carry the source's id, a declared capability, the scan's cluster, a timezone-aware `fetched_at`, at most 8 KiB of raw payload and a stable JSON round trip;
- collecting twice yields the same signal ids;
- v1 recommendations, if any, round-trip through their signals without loss;
- an empty context yields a batch, and the runner records the run as `succeeded`.

The repository's own suite is `tests/sources/contract.py` (`SourceContract` mixin); the example package runs the contract in `examples/greenkube-source-example/tests`.

## Extension points for the next items

| Item | Build on |
|---|---|
| **Consensus (E6, done)** | Emit one signal per target and capability with a `ContainerResourcesProposal` (CPU and memory in one proposal vote in both groups). Set `algorithm.name="vpa-recommender"` (or `algorithm.params.engine_family`) when the source runs the VPA recommender, so it counts as one vote with VPA (`signal.engine`). `kind` should be the v1 finding kind (`RIGHTSIZING_CPU`...) for non-resource proposals. |
| **Identity and revisions (E7, done)** | `fingerprint_v4` and `revision` live on the recommendation; the consensus `votes` list the supporting `signal_id`s per revision. |

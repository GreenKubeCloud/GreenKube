# Robusta KRR (`krr`)

[KRR](https://github.com/robusta-dev/krr) sizes container requests from Prometheus history. GreenKube ingests its JSON reports as signals (roadmap G2).

## What it reads

The output of `krr <strategy> -f json` (KRR 1.x `Result`): every `scans[]` entry becomes up to two signals, one per resource.

| KRR | Signal |
|---|---|
| `object.namespace`, `kind`, `name`, `container` | `target` in this GreenKube's cluster (`object.cluster`, a kubeconfig context name, is kept in `raw`) |
| `recommended.requests.cpu` / `memory` (+ `limits`) | `ContainerResourcesProposal`, kind `RIGHTSIZING_CPU` / `RIGHTSIZING_MEMORY`, decreases **and increases** (CPU cores → millicores, memory bytes) |
| `object.allocations.requests` / `limits` | `proposal.current` |
| `strategy.name`, `strategy.settings` | `algorithm.name = krr.<strategy>`, `algorithm.params` (`cpu_percentile`, `memory_buffer_percentage`, `history_duration`...); the window is `history_duration` hours before the report was received |
| per-resource `severity` | `evidence.data.severity` and a confidence hint: `CRITICAL` 0.8, `WARNING` 0.7, `OK`/`GOOD` 0.6, `UNKNOWN` none. KRR reports no confidence of its own; severity measures how far the request is from the recommendation. |
| `recommended.info` | `evidence.details` (for example `OOMKill detected`) |

A resource with `"?"` (not enough data) or no recommendation is skipped, as is one whose recommendation equals the current request; each skip is counted in the response `diagnostics`. A `null` limit means KRR recommends no limit (`simple` sets no CPU limit): it is noted in `evidence.data.limit_advice`. KRR is in maintenance mode, so the parser ignores unknown fields and skips unreadable entries instead of rejecting the report.

## How it runs

1. A report is pushed to `POST /api/v1/sources/krr/reports` (from CI, or by the optional CronJob below). It is parsed at once and its signals are stored in the inbox; the answer lists the accepted `signal_ids` and the skips. `?complete=false` marks a partial report (for example one namespace): it adds or refreshes advice instead of replacing the previous report.
2. Each scan, the `krr` source replays the signals received within `max_age_hours` (default 168) through the source runner; they are recorded in `source_runs`/`source_signals` (`GET /api/v1/sources/krr/signals`).
3. KRR advice becomes a recommendation only through consensus, where it can agree with VPA, Goldilocks or the native analyzers on the same container.

The `krr` source is `unavailable` until a report was pushed, and `degraded` once the newest report is older than `max_age_hours`.

## Push mode (CI)

```bash
krr simple -f json --logtostderr -p "$PROMETHEUS_URL" > krr.json
curl -sS -X POST "$GREENKUBE_URL/api/v1/sources/krr/reports" \
  -H "Authorization: Bearer $GREENKUBE_SIGNALS_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @krr.json
```

The key needs scope `signals:write`. Reports up to 64 MiB are accepted. When the run covers several clusters, push it to each GreenKube with `?cluster=<kubeconfig context>` (see [Limitations](#limitations)).

## Job mode (optional CronJob)

Disabled by default. Create a `signals:write` API key, store it in a Secret, then:

```yaml
sources:
  krr:
    job:
      enabled: true
      schedule: "17 3 * * *"
      prometheusUrl: http://prometheus-operated.monitoring:9090   # empty: KRR discovers it
      extraArgs: ["--history_duration", "336"]
      apiKeySecret: { name: greenkube-krr, key: api-key }
```

```bash
kubectl -n greenkube create secret generic greenkube-krr --from-literal=api-key=gk_…
```

The CronJob runs the KRR image pinned by digest (`sources.krr.job.image.digest`, `robustadev/krr` v1.31.1 by default) as an init container writing `/report/krr.json` to an `emptyDir`, then a container of the GreenKube image posts it to the API Service. Pods run as non-root with a read-only root filesystem.

**RBAC.** KRR needs cluster-wide read access to what it sizes, plus Prometheus. The chart creates a ServiceAccount `<release>-krr` and a ClusterRole with `get`/`list` on pods, nodes, namespaces and Services (Prometheus discovery), Deployments, StatefulSets, DaemonSets, ReplicaSets, Jobs, CronJobs, HorizontalPodAutoscalers and Argo Rollouts. No Secrets, no writes. Set `sources.krr.job.rbac.create: false` and `serviceAccountName` to bring your own. When `monitoring.networkPolicy.enabled` is set, the API NetworkPolicy admits the job's pods.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `sources.settings.krr.max_age_hours` | `168` | Reports older than this are no longer used. |
| `sources.settings.krr.enabled` | `auto` | `false` ignores pushed reports. |
| `sources.krr.job.*` | disabled | The CronJob above (`schedule`, `strategy`, `image`, `prometheusUrl`, `extraArgs`, `apiKeySecret`, `resources`, `activeDeadlineSeconds`, `rbac`). |

## Limitations

- The parser is pinned against the KRR 1.x report shape (`tests/sources/fixtures/krr_report.json`); newer fields are ignored.
- Pods without a controller are not targets KRR reports; KRR's own savings estimate is not part of its JSON report, so `source_estimate` stays empty (GreenKube recomputes the impact anyway).
- The report is attributed to this GreenKube's cluster; push reports of one cluster to the GreenKube of that cluster. KRR's `object.cluster` is a kubeconfig context name, which GreenKube cannot map to a cluster, so a report covering several clusters (`krr simple --all-clusters`, several `-c`) is refused with `422` (pointer `/scans`, type `multiple_clusters`) rather than guessed. Pass `?cluster=<context>` to keep the entries of the context this GreenKube runs in: the other clusters' entries are skipped and reported in a `other_cluster` warning diagnostic; a context the report does not contain is refused (`cluster_not_in_report`). A report from inside the cluster (the CronJob) names no cluster and needs no parameter.

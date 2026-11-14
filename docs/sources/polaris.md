# Fairwinds Polaris (`polaris`)

[Polaris](https://github.com/FairwindsOps/polaris) audits workloads against best practices. GreenKube ingests its JSON audits as **hygiene findings** (roadmap G4). Polaris finds problems; it never sizes anything. When it reports a missing CPU or memory request, GreenKube computes the request itself from observed usage.

## What it reads

The output of `polaris audit --format=json` (Polaris 9.x and 10.x `AuditData`): every failed check of severity `warning` or `danger` on a controller (`Results`), its pod spec (`PodResult.Results`) or one of its containers (`PodResult.ContainerResults[].Results`) becomes one signal.

| Polaris | Signal |
|---|---|
| `Kind`, `Namespace`, `Name` (+ container name) | `target` in this GreenKube's cluster |
| check `ID` | finding `kind` in upper snake case (`cpuRequestsMissing` → `CPU_REQUESTS_MISSING`), `capability: hygiene` |
| check `Message` | `AdvisoryProposal` text and `evidence.summary` |
| `Severity` | `evidence.data.severity` and a confidence hint: `danger` 0.8, `warning` 0.6 |
| `Category` | `evidence.data.category` (`Efficiency`, `Reliability`, `Security`) |
| `PolarisOutputVersion` | `source_version`, `algorithm.params.output_version` (`algorithm.name = polaris.audit`) |

Passing checks and checks of severity `ignore` are counted in the response `diagnostics` (`passed`, `ignored_severity`), entries without a `Kind` and `Name` are skipped (`invalid_entry`), and unknown fields are ignored.

### Hygiene findings are advice, never pull requests

Findings are `AdvisoryProposal`s: they are stored and listed as the source's signals (`GET /api/v1/sources/polaris/signals`) and never become a recommendation or a pull request by themselves.

### Missing requests: a native follow-up

`cpuRequestsMissing` and `memoryRequestsMissing` also get a **follow-up proposal computed by GreenKube** at scan time:

- GreenKube looks up the container's observed usage in the scan window. With at least `RECOMMENDATION_MIN_SAMPLES` samples, and when it still sees no request on the container, it proposes `p95 × RIGHTSIZING_HEADROOM` (at least `RECOMMENDATION_MIN_CPU_MILLICORES` / `RECOMMENDATION_MIN_MEMORY_BYTES`).
- The proposal is a `container_requests` signal (`RIGHTSIZING_CPU` / `RIGHTSIZING_MEMORY`, `algorithm.name = greenkube.requests_from_usage`, engine family `greenkube.hygiene`) under the `polaris` source id, linked to the finding by `evidence.data.polaris_signal_id`.
- It becomes a recommendation only through consensus, like any other signal. It is a request *increase* (from none), so it is delivered manually until container resources v2.
- A finding without enough usage, or on a container that has a request by now, gets no follow-up; the count is reported in the `follow_up_without_usage` diagnostic.

## How it runs

1. An audit is pushed to `POST /api/v1/sources/polaris/reports` (from CI, or by the optional CronJob below). It is parsed at once and its findings are stored in the inbox; the answer lists the accepted `signal_ids`. `?complete=false` marks a partial audit (for example one namespace): it adds or refreshes findings instead of replacing the previous audit.
2. Each scan, the `polaris` source replays the findings received within `max_age_hours` (default 168), adds the follow-up proposals and records everything in `source_runs`/`source_signals`.

The `polaris` source is `unavailable` until an audit was pushed, and `degraded` once the newest audit is older than `max_age_hours`.

## Push mode (CI)

```bash
polaris audit --format=json --output-file polaris.json
curl -sS -X POST "$GREENKUBE_URL/api/v1/sources/polaris/reports" \
  -H "Authorization: Bearer $GREENKUBE_SIGNALS_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @polaris.json
```

The key needs scope `signals:report` (a `signals:write` key is still accepted for one minor release; the answer then carries `Deprecation: true`). Audits up to 64 MiB are accepted. Errors: `400` (not JSON), `403` (key without `signals:report`), `413` (too large), `422` (not a Polaris audit: no `Results` array, type `polaris_report`), `503` (database unavailable).

## Job mode (optional CronJob)

Disabled by default. Create a `signals:report` API key, store it in a Secret, then:

```yaml
sources:
  polaris:
    job:
      enabled: true
      schedule: "37 3 * * *"
      extraArgs: []            # e.g. ["--namespace", "shop"] (then set complete: false)
      complete: true           # false posts with ?complete=false: a narrowed audit adds findings instead of replacing them all
      apiKeySecret: { name: greenkube-polaris, key: api-key }
```

```bash
kubectl -n greenkube create secret generic greenkube-polaris --from-literal=api-key=gk_…
```

The CronJob runs the Polaris image pinned by digest (`sources.polaris.job.image.digest`, `quay.io/fairwinds/polaris` 10.1.8 by default) as an init container writing `/report/polaris.json` to an `emptyDir`, then a container of the GreenKube image posts it to the API Service. Pods run as non-root with a read-only root filesystem.

**RBAC.** The chart creates a ServiceAccount `<release>-polaris` and a ClusterRole with `get`/`list` on pods, nodes, namespaces, Services, ReplicationControllers, ServiceAccounts, ConfigMaps, Roles, RoleBindings, ClusterRoles, ClusterRoleBindings, Deployments, StatefulSets, DaemonSets, ReplicaSets, Jobs, CronJobs, HorizontalPodAutoscalers, PodDisruptionBudgets, Ingresses, NetworkPolicies and Argo Rollouts. `polaris audit` lists every kind its built-in checks target and fails on the first forbidden list. No Secrets, no writes. Set `sources.polaris.job.rbac.create: false` and `serviceAccountName` to bring your own (for example one bound to the built-in `view` ClusterRole). When `monitoring.networkPolicy.enabled` is set, the API NetworkPolicy admits the job's pods.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `sources.settings.polaris.max_age_hours` | `168` | Audits older than this are no longer used. |
| `sources.settings.polaris.enabled` | `auto` | `false` ignores pushed audits. |
| `sources.polaris.job.*` | disabled | The CronJob above (`schedule`, `image`, `extraArgs`, `complete`, `apiKeySecret`, `resources`, `activeDeadlineSeconds`, `rbac`, `serviceAccountName`). |

## Limitations

- The parser is pinned against the Polaris audit shape described above (`tests/sources/fixtures/polaris_report.json`, a synthetic fixture labelled as such); Polaris's own `Score` is not used.
- The audit is attributed to this GreenKube's cluster: push the audit of one cluster to the GreenKube of that cluster.
- Popeye (`popeye -o json`) is not ingested yet; push its findings through the [Push API](push.md) as `hygiene` signals in the meantime.
- The UI has no dedicated "Hygiene" tab yet: the findings are listed with the source's signals.

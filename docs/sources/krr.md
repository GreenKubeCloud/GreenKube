# Robusta KRR (`krr`)

[KRR](https://github.com/robusta-dev/krr) sizes container requests from Prometheus history. GreenKube does not run KRR: you run it where you like (CI, a laptop, a CronJob of your own) and push its JSON report to GreenKube's push API, which turns it into signals (roadmap G2). This page has a ready-made CronJob.

KRR advice never sets a value on its own. GreenKube's measured value decides the size; KRR's proposal moves the confidence up when it agrees, down when it disagrees, and a container KRR sizes but GreenKube does not stays a signal (see [Consensus across sources](../recommendation.md#consensus-across-sources)).

## The push contract

`POST /api/v1/sources/krr/reports` takes the raw output of `krr <strategy> -f json` (KRR 1.x `Result`), unchanged:

| Part | Value |
|---|---|
| Body | The KRR JSON report, `Content-Type: application/json`, up to 64 MiB. |
| Authentication | `Authorization: Bearer <API key>` with scope `signals:report` (create it in Settings > API keys, or `POST /api/v1/auth/api-keys` with `{"name": "krr", "scopes": ["signals:report"]}`). A `signals:write` key is still accepted for one minor release; the answer then carries `Deprecation: true`. |
| `?complete=` | `true` (default): the report covers every workload KRR sizes and replaces the previous report. `false`: a partial report (for example `--namespace shop`) that only adds or refreshes advice. |
| `?cluster=` | Only for a report that covers several clusters (`--all-clusters`, several `-c`): the kubeconfig context of the cluster this GreenKube runs in. A report made inside the cluster names no cluster and needs no parameter. |
| Answer `200` | `{"batch_id", "source_id": "krr", "complete", "received_at", "accepted", "signal_ids", "withdrawn", "diagnostics"}`: the diagnostics count the skipped entries (`"?"` values, unchanged requests, other clusters). |
| Errors | `400` not JSON, `403` key without the scope, `409` partial reports would hold more than 50,000 signals (push a complete one), `413` body too large, `422` not a KRR report (pointer and `type`: `krr_report`, `multiple_clusters`, `cluster_not_in_report`), `503` database unavailable. |

The signals are stored at once and used by the next scans. Any other tool uses the generic batch endpoint `POST /api/v1/signals` instead ([Push API](push.md)).

## Example: a CronJob that runs KRR and pushes its report

The manifest runs KRR once a day in the `greenkube` namespace, next to a release named `greenkube` (API Service `greenkube-api`, port 8000). KRR writes its report to a shared `emptyDir`; a second container posts it with `curl`. Adjust the namespace, the Service name, the Prometheus URL and the schedule.

1. Create the API key and store it in a Secret:

   ```bash
   kubectl -n greenkube create secret generic greenkube-krr --from-literal=api-key=gk_…
   ```

2. Apply the manifest:

   ```yaml
   apiVersion: v1
   kind: ServiceAccount
   metadata:
     name: greenkube-krr
     namespace: greenkube
   ---
   # KRR reads workloads, their pods and HPAs across the cluster, plus Services to
   # discover Prometheus. Read-only; no Secrets.
   apiVersion: rbac.authorization.k8s.io/v1
   kind: ClusterRole
   metadata:
     name: greenkube-krr
   rules:
     - apiGroups: [""]
       resources: ["pods", "nodes", "namespaces", "services"]
       verbs: ["get", "list"]
     - apiGroups: ["apps"]
       resources: ["deployments", "statefulsets", "daemonsets", "replicasets"]
       verbs: ["get", "list"]
     - apiGroups: ["batch"]
       resources: ["jobs", "cronjobs"]
       verbs: ["get", "list"]
     - apiGroups: ["autoscaling"]
       resources: ["horizontalpodautoscalers"]
       verbs: ["get", "list"]
     - apiGroups: ["argoproj.io"]
       resources: ["rollouts"]
       verbs: ["get", "list"]
   ---
   apiVersion: rbac.authorization.k8s.io/v1
   kind: ClusterRoleBinding
   metadata:
     name: greenkube-krr
   roleRef:
     apiGroup: rbac.authorization.k8s.io
     kind: ClusterRole
     name: greenkube-krr
   subjects:
     - kind: ServiceAccount
       name: greenkube-krr
       namespace: greenkube
   ---
   apiVersion: batch/v1
   kind: CronJob
   metadata:
     name: greenkube-krr
     namespace: greenkube
   spec:
     schedule: "17 3 * * *"
     concurrencyPolicy: Forbid
     successfulJobsHistoryLimit: 1
     failedJobsHistoryLimit: 3
     jobTemplate:
       spec:
         backoffLimit: 1
         activeDeadlineSeconds: 3600
         ttlSecondsAfterFinished: 86400
         template:
           metadata:
             labels:
               app.kubernetes.io/name: greenkube-krr
           spec:
             restartPolicy: Never
             serviceAccountName: greenkube-krr
             securityContext:
               runAsNonRoot: true
               runAsUser: 10001
               runAsGroup: 10001
               seccompProfile:
                 type: RuntimeDefault
             initContainers:
               - name: krr
                 # Pin the image by digest (the multi-arch index of the tag you choose).
                 image: robustadev/krr:v1.31.1@sha256:835bf3b6fec4d8f27d96d236a36542bd87bfcb4f12f3537f8dce7f886916ed9c
                 command:
                   - python
                   - krr.py
                   - simple
                   - -f
                   - json
                   - --fileoutput
                   - /report/krr.json
                   - --logtostderr
                   - -p
                   - http://prometheus-operated.monitoring:9090   # leave out -p and this line to let KRR discover Prometheus
                   - --history_duration
                   - "336"
                 env:
                   - name: HOME
                     value: /tmp
                 securityContext:
                   allowPrivilegeEscalation: false
                   readOnlyRootFilesystem: true
                   capabilities:
                     drop: ["ALL"]
                 resources:
                   requests: {cpu: 100m, memory: 512Mi}
                   limits: {memory: 2Gi}
                 volumeMounts:
                   - {name: report, mountPath: /report}
                   - {name: tmp, mountPath: /tmp}
             containers:
               - name: push
                 image: curlimages/curl:8.10.1
                 command:
                   - sh
                   - -c
                   - >-
                     curl -sS --fail-with-body -X POST
                     "http://greenkube-api.greenkube.svc:8000/api/v1/sources/krr/reports"
                     -H "Authorization: Bearer ${GREENKUBE_SIGNALS_KEY}"
                     -H "Content-Type: application/json"
                     --data-binary @/report/krr.json
                 env:
                   - name: GREENKUBE_SIGNALS_KEY
                     valueFrom:
                       secretKeyRef: {name: greenkube-krr, key: api-key}
                 securityContext:
                   allowPrivilegeEscalation: false
                   readOnlyRootFilesystem: true
                   capabilities:
                     drop: ["ALL"]
                 resources:
                   requests: {cpu: 10m, memory: 32Mi}
                   limits: {memory: 128Mi}
                 volumeMounts:
                   - {name: report, mountPath: /report, readOnly: true}
             volumes:
               - {name: report, emptyDir: {}}
               - {name: tmp, emptyDir: {}}
   ```

3. Run it once and read the answer:

   ```bash
   kubectl -n greenkube create job --from=cronjob/greenkube-krr krr-now
   kubectl -n greenkube logs job/krr-now -c push
   ```

   The answer lists the accepted signals; `GET /api/v1/sources/krr/signals` shows them after the next scan.

Every pod runs as non-root with a read-only root filesystem, so the job passes the `restricted` Pod Security profile. When the chart's `monitoring.networkPolicy.enabled` is set, admit the job's pods to the API:

```yaml
monitoring:
  networkPolicy:
    additionalIngress:
      - from:
          - podSelector:
              matchLabels:
                app.kubernetes.io/name: greenkube-krr
        ports:
          - {protocol: TCP, port: 8000}
```

From CI, the same push is two commands:

```bash
krr simple -f json --logtostderr -p "$PROMETHEUS_URL" > krr.json
curl -sS --fail-with-body -X POST "$GREENKUBE_URL/api/v1/sources/krr/reports" \
  -H "Authorization: Bearer $GREENKUBE_SIGNALS_KEY" \
  -H "Content-Type: application/json" \
  --data-binary @krr.json
```

When the run covers several clusters, push it to each GreenKube with `?cluster=<kubeconfig context>` (see [Limitations](#limitations)).

## What it reads

Every `scans[]` entry of the report becomes up to two signals, one per resource.

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

1. A pushed report is parsed at once and its signals are stored in the inbox (migration `0070`).
2. Each scan, the `krr` source reads the signals received within `max_age_hours` (default 168) through the source runner; they are recorded in `source_runs`/`source_signals` (`GET /api/v1/sources/krr/signals`).
3. KRR advice reaches a recommendation only through consensus with GreenKube's own measurement: **KRR alone never creates a recommendation**, and KRR never sets the value. A container with no native GreenKube proposal keeps KRR's advice as a signal, visible on the Sources page. In the field, 78 KRR-only recommendations flooded the list, for example a 20 Mi `liveness-probe` sidecar of `csi-nfs-controller` raised to 100 Mi on KRR's word alone.

KRR's signals are pushed: together with every other pushed signal they count as at most one independent consensus vote.

The `krr` source is `unavailable` until a report was pushed, and `degraded` once the newest report is older than `max_age_hours`.

## Floors and small changes

KRR `simple` never proposes less than `--mem-min` (100 MB, which KRR reads as MiB) and `--cpu-min` (10m). A KRR value equal to that minimum is its floor, not a measured need, so it is **not counted as a vote** (51 of 94 memory recommendations sat exactly on 100 Mi in the field). Set `sources.toolFloors.krr` (`SIGNAL_TOOL_FLOORS`, default `{cpu: 10m, memory: 100Mi}`) to the minimums your KRR runs with. A KRR value that changes the current request by less than `sources.minChange` (5 % and 10m CPU or 16 MiB by default) counts as a "keep the current request" vote instead of its own value. Both are listed in the recommendation's `consensus.ignored_votes`.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `sources.settings.krr.max_age_hours` | `168` | Reports older than this are no longer used. |
| `sources.settings.krr.enabled` | `auto` | `false` ignores pushed reports. |
| `sources.toolFloors.krr` | `{cpu: 10m, memory: 100Mi}` | KRR's `--cpu-min`/`--mem-min`: a value on them is not a consensus vote. |
| `sources.minChange.*` | 5 %, 10m, 16 MiB | Smallest change a KRR value must propose to vote for its own value; a smaller one votes to keep the current request. |

## Limitations

- The parser is pinned against the KRR 1.x report shape (`tests/sources/fixtures/krr_report.json`); newer fields are ignored.
- Pods without a controller are not targets KRR reports; KRR's own savings estimate is not part of its JSON report, so `source_estimate` stays empty (GreenKube recomputes the impact anyway).
- The report is attributed to this GreenKube's cluster; push reports of one cluster to the GreenKube of that cluster. KRR's `object.cluster` is a kubeconfig context name, which GreenKube cannot map to a cluster, so a report covering several clusters (`krr simple --all-clusters`, several `-c`) is refused with `422` (pointer `/scans`, type `multiple_clusters`) rather than guessed. Pass `?cluster=<context>` to keep the entries of the context this GreenKube runs in: the other clusters' entries are skipped and reported in a `other_cluster` warning diagnostic; a context the report does not contain is refused (`cluster_not_in_report`). A report from inside the cluster (the CronJob above) names no cluster and needs no parameter.

# Your first GreenKube pull request

This guide runs the whole GreenKube loop on a local cluster in about 20 minutes:

```text
collection ─► recommendation ─► plan preview ─► approval ─► pull request
   ▲                                                            │ merge
   └─ measurement ◄─ apply detection ◄─ sync confirmation ◄─ GitOps sync
```

A sample application with deliberately oversized CPU and memory requests is
deployed from a Git repository by Argo CD (and, optionally, by Flux). GreenKube
observes it, recommends smaller requests, opens a pull request against the
repository, follows the merge through the GitOps controller and starts
measuring the change.

Everything is scripted in `hack/e2e/` and runs with one command, `make e2e`.
The same scripts serve as the end-to-end test of GreenKube (roadmap O1); the
steps below explain what they do so you can repeat them by hand.

> **Demo settings.** The quickstart values collect a sample every 30 seconds,
> accept 6 samples (3 minutes) as enough evidence and measure a merged change
> over 6 minutes. Never use them in production: the defaults require 36 samples
> and measure over 72 hours for good reasons.

## Prerequisites

A Kubernetes cluster with:

| Component | Used for | Default location |
|---|---|---|
| Prometheus (kube-prometheus) with cAdvisor and kube-state-metrics | Usage and request samples | `http://prometheus-k8s.monitoring.svc.cluster.local:9090` |
| Metrics Server | Per-container usage | `kube-system` |
| Argo CD | GitOps delivery of the sample app | namespace `argocd` |
| Flux (optional) | Second delivery path | namespace `flux-system` |
| Gitea | Git server for the sample repository | `http://gitea-http.gitea.svc.cluster.local:3000` |
| OpenCost (optional) | Observed costs | `http://opencost.opencost.svc.cluster.local:9003` |

The harness targets Minikube with the Docker runtime: it builds the GreenKube
image inside Minikube (`eval $(minikube docker-env)`), so no registry is needed.
On a workstation you also need `kubectl`, `helm`, `curl`, `jq` and `minikube`.

You need a Gitea API token with read/write repository scope for the user that
will own the sample repository (default user `greenkube`). Pass it as
`GITEA_TOKEN`, or as a file with `GITEA_TOKEN_FILE`.

## Run it

```bash
export GITEA_TOKEN=<gitea token>
make e2e
```

`make e2e` starts from a clean slate: it removes what a previous run left (the
GreenKube release and its database, the sample repository, the sample namespace
and the harness's Argo CD/Flux objects), then runs the steps below. It leaves the
environment running so you can explore it; `make e2e-status` prints the result
of every step and `make e2e-clean` removes everything.

Every setting is an environment variable with a default in `hack/e2e/lib.sh`.
The most useful ones:

| Variable | Default | Meaning |
|---|---|---|
| `GITEA_TOKEN` / `GITEA_TOKEN_FILE` | — | Gitea token of `GITEA_OWNER` |
| `GITEA_OWNER` / `GITEA_REPO` | `greenkube` / `greenkube-e2e-quickstart` | Sample repository (created and deleted by the harness) |
| `GK_NAMESPACE` / `GK_RELEASE` | `greenkube` / `greenkube` | Where GreenKube is installed |
| `GK_IMAGE_TAG` | `e2e-dev` | Tag of the image built from your checkout |
| `GK_SKIP_BUILD` | `0` | `1` reuses an image already in Minikube |
| `GK_EXTRA_VALUES` | — | Extra Helm values file |
| `E2E_FLUX` | `1` | `0` skips the Flux path |
| `E2E_FRESH` | `1` | `0` keeps the previous release, repository and data |
| `E2E_WAIT_VERDICT` | `1` | `0` stops once the measurement has started |
| `E2E_STATE_DIR` | `$TMPDIR/greenkube-e2e` | Port-forward logs and the evidence of each step |

The scripts can also run one at a time, in this order: `build-image.sh`,
`setup-repo.sh`, `setup-gitops.sh`, `install.sh`, `loop.sh argocd`,
`loop.sh flux`. Each one is idempotent.

## What happens

### 1. Build and install

`hack/e2e/build-image.sh` builds `greenkube/greenkube:e2e-dev` from your
checkout inside Minikube. `hack/e2e/install.sh` creates the Secret
`greenkube-git` (key `token`) with the Gitea token and installs the chart with
[`docs/examples/quickstart/values.yaml`](../examples/quickstart/values.yaml):

```bash
kubectl -n greenkube create secret generic greenkube-git --from-literal=token="$GITEA_TOKEN"
helm upgrade --install greenkube ./helm-chart -n greenkube --create-namespace \
  -f docs/examples/quickstart/values.yaml \
  --set config.prometheus.url=http://prometheus-k8s.monitoring.svc.cluster.local:9090
```

The values turn on GitOps automation with one Gitea provider whose allowlist is
the sample repository only, and accelerate evidence:

| Setting | Quickstart | Default |
|---|---|---|
| `config.prometheus.queryRangeStep` (`PROMETHEUS_QUERY_RANGE_STEP`) | `30s` | `5m` |
| `config.collection.maxBackfill` | `30m` | `24h` |
| `config.recommendations.minSamples` (`RECOMMENDATION_MIN_SAMPLES`) | `6` | `36` |
| `config.recommendations.scanInterval` | `1m` | `1h` |
| `config.recommendations.verification.windowHours` | `0.1` (6 minutes) | `72` |
| `config.recommendations.verification.lifecycleInterval` | `30s` | `5m` |
| `automation.policy.minSamples` / `minCoverage` | `6` / `0` | `36` / `0.5` |
| `automation.discoveryInterval` / `prPollIntervalSeconds` / `syncPollIntervalSeconds` | `30s` / `10` / `10` | `5m` / `120` / `30` |

`minCoverage: 0` is needed because coverage is measured against the whole
lookback window (at least one day), which a fresh install cannot fill.

### 2. Sample repository and GitOps controllers

`hack/e2e/setup-repo.sh` creates `greenkube/greenkube-e2e-quickstart` on Gitea
and commits [`docs/examples/quickstart/gitops-repo/`](../examples/quickstart/gitops-repo/)
through the Gitea contents API. `hack/e2e/setup-gitops.sh` creates the namespace
`greenkube-e2e-apps` and registers:

- the Argo CD Application `greenkube-e2e-quickstart` (raw YAML,
  `apps/oversized-web`, automated sync);
- with Flux, the GitRepository and Kustomization `greenkube-e2e-quickstart`
  (Kustomize, `flux/oversized-worker`).

`oversized-web` requests `400m` CPU and `384Mi` memory; `oversized-worker`
requests `300m` and `256Mi`. Both use almost nothing.

The Flux overlay declares `namespace: greenkube-e2e-apps`, the same namespace as
the Kustomization's `targetNamespace`. GreenKube plans a Flux `targetNamespace`
only in that case; without it the recommendation reports `unsupported_source`.

### 3. The loop

`hack/e2e/loop.sh argocd` (then `loop.sh flux`) drives one workload through the
loop with the public APIs only. The calls below are what it sends; run them
yourself after `kubectl -n greenkube port-forward svc/greenkube-api 8000` and

```bash
export GREENKUBE_API_KEY="$(kubectl -n greenkube get secret greenkube -o jsonpath='{.data.GREENKUBE_API_KEY}' | base64 -d)"
api() { curl -fsS -H "Authorization: Bearer $GREENKUBE_API_KEY" -H 'Content-Type: application/json' "http://localhost:8000/api/v1$1" "${@:2}"; }
```

| Step | How the harness checks it |
|---|---|
| Automation ready | `api /automation/status` → `ready: true`, `token_configured: true` |
| Binding | `api /gitops/bindings` → `greenkube-e2e-apps/oversized-web` is `resolved` to the sample repository |
| Collection and recommendation | `api /optimization/runs -X POST -d '{"namespace":"greenkube-e2e-apps"}'` queues a scan; `api '/recommendations/active?namespace=greenkube-e2e-apps'` lists the `RIGHTSIZING_CPU`/`RIGHTSIZING_MEMORY` recommendations; `api /recommendations/<id>/apply-pr/eligibility` → `eligible: true` once 6 samples are in (3–5 minutes) |
| Plan preview | `api /automation/plans -X POST -d '{"recommendation_ids":[<ids>]}'` → status `validated`, a `sha256:` digest and the diff |
| Approval | `api /automation/plans/<plan_id>/approve -X POST -d '{"digest":"sha256:…"}'` → an `operation_id` |
| Pull request | `api /automation/operations/<operation_id>` → `succeeded`; `api /automation/pull-requests` gives the PR number and URL |
| Merge | Gitea API `POST /repos/greenkube/greenkube-e2e-quickstart/pulls/<n>/merge` with `{"Do":"merge"}` (or the Gitea UI) |
| GitOps sync | Argo CD Application `status.sync.revision` equals the merge commit and is `Synced`/`Healthy` (Flux: Kustomization `lastAppliedRevision`) |
| Sync confirmation | `api /automation/plans/<plan_id>` → `merged`, then `synced` |
| Apply detection | `api /recommendations/<id>` → status `applied`, `application_method` set |
| Measurement | `api /recommendations/<id>` → `verification_status` set; after the 6-minute window, a verdict (`verified`, `not_realized`, `inconclusive`…) |

Each step is recorded in `$E2E_STATE_DIR/evidence/steps.jsonl` with the API
responses next to it, and `make e2e-status` prints them.

Typical timings on Minikube: the first eligible recommendation 3–4 minutes after
GreenKube starts, the pull request about 5 seconds after approval, sync
confirmation and apply detection within a minute of the merge, and a
measurement verdict about 10 minutes after the merge (6-minute window plus
sample collection; the verifier extends the window when it has fewer than 6
trusted samples).

The Flux pull request does not edit the workload manifest: it adds a strategic
merge patch under `flux/oversized-worker/greenkube-patches/` and references it
from `kustomization.yaml`.

If a run stops while a GreenKube pull request is still open, `loop.sh` resumes
from that pull request instead of opening a second one.

## Explore

- **Gitea:** `kubectl -n gitea port-forward svc/gitea-http 3000` and open
  `http://localhost:3000/greenkube/greenkube-e2e-quickstart/pulls?state=closed`.
- **GreenKube UI:** `kubectl -n greenkube port-forward svc/greenkube-api 8000`,
  open `http://localhost:8000` and paste the API key. The Recommendations page
  shows the applied recommendations and their measurement.
- **Argo CD:** `kubectl -n argocd get application greenkube-e2e-quickstart`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Image build fails while downloading Helm or Kustomize | The Dockerfile fetches pinned renderer binaries from `get.helm.sh` and GitHub releases; retry later, or rerun with `GK_SKIP_BUILD=1` to reuse an image already in Minikube |
| No recommendation after 10 minutes | Prometheus URL and scrape of cAdvisor/kube-state-metrics; `kubectl -n greenkube logs deploy/greenkube -c greenkube` |
| Recommendation not eligible | `policy_result.checks` in the eligibility response: confidence grows with the number of samples |
| Binding not resolved | The provider `apiBaseUrl` host must be the host of the Argo CD/Flux repository URL, and the repository must be in `repositoryAllowlist` |
| Plan stays `merged` | The GitOps controller has not synced the merge commit yet; check the Application or Kustomization |
| PR link unreachable from the browser | Set `automation.providers[].webBaseUrl` to the browser-facing Gitea origin |

## Clean up

```bash
make e2e-clean
```

It removes the GreenKube release and namespace (database and generated
credentials included), the sample namespace, the harness's Argo CD Application
and Flux objects, and the sample repository. Nothing else is touched.

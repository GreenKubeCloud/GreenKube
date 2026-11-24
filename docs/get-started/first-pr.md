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

You need a Gitea API token with read/write repository scope. Its user must be
the owner of the sample repository (`GITEA_OWNER`, default `greenkube`), a
member of that organization, or a Gitea administrator. When the repository
belongs to the token's own user, Gitea also requires the `write:user` scope to
create it: a fresh run (`E2E_FRESH=1`, `GITEA_RESET_REPO=1`) checks that scope
and stops before deleting anything when it is missing. Pass the token as
`GITEA_TOKEN`, or as a file with `GITEA_TOKEN_FILE`; there is no default.

## Run it

Use a disposable cluster. The harness installs, upgrades and deletes objects,
so it checks two things before it changes anything:

- **The kubectl context.** Every script stops unless `kubectl config
  current-context` is `minikube`. Set `E2E_KUBE_CONTEXT` to use another
  disposable cluster's context.
- **Ownership.** The harness labels everything it creates with
  `greenkube.io/e2e=true` (the namespaces `greenkube` and `greenkube-e2e-apps`,
  the Argo CD Application and the Flux objects) and writes `managed by
  hack/e2e` in the sample repository's description. It never updates or deletes
  an object with one of its names that lacks the marker: it stops, or with
  `make e2e-clean` reports the object and leaves it alone. In particular it
  never installs over, or deletes, a GreenKube release in a `greenkube`
  namespace that it did not create. `E2E_ADOPT=1` takes such objects over,
  for example an environment from a version of the harness without the marker.

To install GreenKube next to the sample app and open the first pull request
yourself from the UI, without deleting anything:

```bash
export GITEA_TOKEN=<gitea token>
make quickstart
```

To run the whole loop unattended:

```bash
export GITEA_TOKEN=<gitea token>
make e2e
```

> **`make e2e` is destructive by default (`E2E_FRESH=1`).** It first removes
> what a previous run created: the GreenKube release and its namespace,
> **including the PostgreSQL volume and the generated API key**, the sample
> repository, the sample namespace and the harness's Argo CD/Flux objects. Only
> objects that carry the ownership marker are removed. Use `E2E_FRESH=0` to keep
> them.

`make e2e` then runs the steps below. It leaves the environment running so you
can explore it; `make e2e-status` prints the result of every step and
`make e2e-clean` removes everything the harness created.

Every setting is an environment variable with a default in `hack/e2e/lib.sh`
(or in `run.sh` and `loop.sh` for the run options). The most useful ones:

| Variable | Default | Meaning |
|---|---|---|
| `GITEA_TOKEN` / `GITEA_TOKEN_FILE` | — (required) | Gitea token with access to `GITEA_OWNER` |
| `GITEA_OWNER` / `GITEA_REPO` | `greenkube` / `greenkube-e2e-quickstart` | Sample repository (created and deleted by the harness) |
| `GITEA_WEB_URL` | `http://localhost:3000` | Browser-facing Gitea origin used in pull-request links |
| `GK_NAMESPACE` / `GK_RELEASE` | `greenkube` / `greenkube` | Where GreenKube is installed |
| `GK_IMAGE_TAG` | `e2e-dev` | Tag of the image built from your checkout |
| `GK_SKIP_BUILD` | `0` | `1` reuses an image already in Minikube |
| `GK_EXTRA_VALUES` | — | Extra Helm values file |
| `E2E_KUBE_CONTEXT` | `minikube` | The only kubectl context the harness runs against |
| `E2E_ADOPT` | `0` | `1` takes over objects with the harness's names that lack its ownership marker |
| `E2E_ARGOCD` | `1` | `0` skips the Argo CD path |
| `E2E_FLUX` | `1` | `0` skips the Flux path |
| `E2E_FRESH` | `1` | `1` deletes the previous run's release, data, repository and namespaces first; `0` keeps them |
| `E2E_RESUME_MAX_AGE` | `21600` | With `E2E_FRESH=0`, the age in seconds beyond which a merged pull request is not resumed |
| `E2E_WAIT_VERDICT` | `1` | `0` stops once the measurement has started |
| `E2E_TIMEOUT_VERDICT` | `1500` | Seconds to wait for the measurement verdict |
| `E2E_MIN_COLLECTION_SECONDS` | `900` | With a verdict, seconds GreenKube collects before the first change, so the baseline window before it is collected live |
| `E2E_ALLOW_REGRESSION` | `0` | `1` records a verdict other than `verified`/`passed` without failing the run |
| `E2E_STATE_DIR` | `$TMPDIR/greenkube-e2e` | Port-forward logs and the evidence of each step |

The sample namespace is always `greenkube-e2e-apps`: the Argo CD sample
manifests and destination declare it, and the Flux Kustomization's
`targetNamespace` sets it for the Flux sample.

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

The values declare one Gitea provider whose allowlist is the sample repository
only, and accelerate evidence:

| Setting | Quickstart | Default |
|---|---|---|
| `config.prometheus.queryRangeStep` (`PROMETHEUS_QUERY_RANGE_STEP`) | `30s` | `5m` |
| `config.collection.maxBackfill` | `30m` | `24h` |
| `config.recommendations.lookbackDays` | `1` | `14` |
| `config.recommendations.minSamples` (`RECOMMENDATION_MIN_SAMPLES`, every sample gate) | `6` | `36` |
| `config.recommendations.minDecreaseCoverageDays` | `0.001` | `7` |
| `config.recommendations.scanInterval` | `1m` | `1h` |
| `config.recommendations.verification.windowHours` | `0.1` (6 minutes) | `72` |
| `config.recommendations.verification.lifecycleInterval` | `30s` | `5m` |
| `automation.policy.minCoverage` / `cooldownMinutes` / `missingEvidence` | `0` / `0` / `warn` | `0.5` / `60` / `fail` |
| `automation.discoveryInterval` / `prPollIntervalSeconds` / `syncPollIntervalSeconds` | `30s` / `10` / `10` | `5m` / `120` / `30` |
| `automation.syncTimeoutSeconds` | `900` | `1800` |

`minCoverage: 0` keeps collection gaps of a fresh demo install from holding the
plan back (the coverage gate is evaluated against the data collected since
install).

The quickstart decides merged changes with the 6-minute verifier, so change
measurement (`config.recommendations.changeMeasurement.enabled`) is off and
`/changes` stays empty. To record merged changes and measure them against a
counterfactual on accelerated demo windows (a 6-hour baseline and a 72-minute
post-window, decided about 80 minutes after the merge), add
`-f docs/examples/quickstart/values-change-measurement.yaml`. Accelerated
measurements use 5-minute slots, the baseline mean as counterfactual and demo
thresholds, so they reach a decided outcome; each records
`representative: false` (a demonstration of the pipeline, not a saving). Run
`make e2e` with an `E2E_TIMEOUT_VERDICT` above two hours with that overlay
(and `E2E_ALLOW_REGRESSION=1` if the outcome may be inconclusive). The savings ledger
(`/api/v2/ledger/*`) fills and is verified every 5 minutes either way.

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

The Flux overlay is a plain Kustomize overlay without a namespace: the
Kustomization's `targetNamespace` puts the worker in `greenkube-e2e-apps`.
GreenKube renders the overlay the way Flux builds it (with `targetNamespace`,
`namePrefix` and `nameSuffix` applied) to find the live workload, and writes the
patch selector with the name and namespace the workload has inside the overlay,
before those transformers run (see [Flux Kustomizations](../automation.md#flux-kustomizations)).

### 3. The loop

`hack/e2e/loop.sh argocd` (then `loop.sh flux`) drives one workload through the
loop with the public APIs only. The calls below are what it sends; run them
yourself after `kubectl -n greenkube port-forward svc/greenkube-api 8000` and

```bash
# install.sh supplies the harness key as secrets.apiKey (Secret greenkube-values): browser
# auth modes no longer generate a bootstrap key. Outside the harness, create a scoped key
# in Settings > API keys instead.
export GREENKUBE_API_KEY="$(kubectl -n greenkube get secret greenkube-values -o jsonpath='{.data.GREENKUBE_API_KEY}' | base64 -d)"
api() { curl -fsS -H "Authorization: Bearer $GREENKUBE_API_KEY" -H 'Content-Type: application/json' "http://localhost:8000/api/v1$1" "${@:2}"; }
```

| Step | How the harness checks it |
|---|---|
| Automation ready | `api /automation/status` → `ready: true`, `token_configured: true` |
| Binding | `api /gitops/bindings` → `greenkube-e2e-apps/oversized-web` is `resolved` to the sample repository |
| Collection and recommendation | `api /optimization/runs -X POST -d '{"namespace":"greenkube-e2e-apps"}'` queues a scan; `api '/recommendations/active?namespace=greenkube-e2e-apps'` lists the `RIGHTSIZING_CPU`/`RIGHTSIZING_MEMORY` recommendations; each one's `eligibility.can_open_pr` becomes `true` once 6 samples are in (3–5 minutes) |
| Plan preview | `api /automation/plans -X POST -d '{"recommendation_ids":[<ids>]}'` → status `validated`, a `sha256:` digest and the diff |
| Approval | `api /automation/plans/<plan_id>/approve -X POST -d '{"digest":"sha256:…"}'` → an `operation_id` |
| Pull request | `api /automation/plans/<plan_id>` → `operation_status: succeeded` (plan `pr_open`); `api /automation/pull-requests` gives the PR number and URL |
| Merge | Gitea API `POST /repos/greenkube/greenkube-e2e-quickstart/pulls/<n>/merge` with `{"Do":"merge"}` (or the Gitea UI) |
| GitOps sync | Argo CD Application `status.sync.revision` equals the merge commit and is `Synced`/`Healthy` (Flux: Kustomization `lastAppliedRevision`) |
| Sync confirmation | `api /automation/plans/<plan_id>` → `merged`, then `synced` |
| Apply detection | `api /recommendations/<id>` → status `applied`, `application_method` set |
| Measurement | `api /recommendations/<id>` → `verification_status` set; after the 6-minute window, a verdict. The run passes only on `verified`/`passed`; any other verdict (`regressed`, `rollback_review`, `not_realized`, `inconclusive`…) is recorded as `verdict_<status>_<verification_status>` and fails the run unless `E2E_ALLOW_REGRESSION=1` |

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

With `E2E_FRESH=0`, `loop.sh` resumes from the workload's latest unfinished
GreenKube pull request instead of opening a second one: from the merge when it
is still open, from the GitOps sync when it is merged but its plan is not
`applied` yet. It finds the pull request through
`api '/recommendations/history?namespace=greenkube-e2e-apps&start=…&end=…'`,
whatever status the recommendations have. A merged pull request whose plan is
already `applied` (a loop that went through apply detection), or that was merged
more than `E2E_RESUME_MAX_AGE` seconds ago, is ignored: the run starts a new loop
instead of waiting on an old change (`hack/e2e/resumable-pr.jq`). The sync check accepts the merge
commit or any later commit that contains it. The merge retries only while Gitea
answers 405 (mergeability not computed yet); a closed pull request or any other
error stops the run at once.

## Explore

- **Gitea:** `kubectl -n gitea port-forward svc/gitea-http 3000` and open
  `http://localhost:3000/greenkube/greenkube-e2e-quickstart/pulls?state=closed`.
- **GreenKube UI:** `kubectl -n greenkube port-forward svc/greenkube-api 8000`,
  open `http://localhost:8000` and sign in as `admin` with the initial password
  (see [Authentication: first sign-in](../authentication.md#first-sign-in)). The Recommendations page
  shows the applied recommendations and their measurement.
- **Argo CD:** `kubectl -n argocd get application greenkube-e2e-quickstart`.

## Troubleshooting

| Symptom | Check |
|---|---|
| Image build fails while downloading Helm or Kustomize | The Dockerfile fetches pinned renderer binaries from `get.helm.sh` and GitHub releases; retry later, or rerun with `GK_SKIP_BUILD=1` to reuse an image already in Minikube |
| No recommendation after 10 minutes | Prometheus URL and scrape of cAdvisor/kube-state-metrics; `kubectl -n greenkube logs deploy/greenkube-controller -c controller` |
| Recommendation not eligible | `eligibility.blockers` on the recommendation (`code`, `message`, `fix`; `warming_up` gives `eligible_at`): confidence grows with the number of samples. A rejected plan preview lists the failed gates in `policy_result.checks` |
| Binding not resolved | The provider `apiBaseUrl` host must be the host of the Argo CD/Flux repository URL, and the repository must be in `repositoryAllowlist` |
| Plan stays `merged` | The GitOps controller has not synced the merge commit yet; check the Application or Kustomization |
| PR link unreachable from the browser | The quickstart sets `automation.providers[].webBaseUrl` to `http://localhost:3000` (`GITEA_WEB_URL`): run `kubectl -n gitea port-forward svc/gitea-http 3000`, or set it to your browser-facing Gitea origin |

## Clean up

```bash
make e2e-clean
```

It removes the GreenKube release and namespace (database and generated
credentials included), the sample namespace, the harness's Argo CD Application
and Flux objects, and the sample repository, but only those that carry the
ownership marker, and only on the `E2E_KUBE_CONTEXT` context. Nothing else is
touched.

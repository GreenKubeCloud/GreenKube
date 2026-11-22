# GreenKube quickstart GitOps repository

Sample application repository for the GreenKube quickstart. Push this directory
to a Git server that GreenKube can open pull requests against.

| Path | Reconciled by | Layout | Workload |
|---|---|---|---|
| `apps/oversized-web/` | Argo CD Application | raw YAML | `Deployment/oversized-web` |
| `flux/oversized-worker/` | Flux Kustomization | Kustomize | `Deployment/oversized-worker` |

Both workloads request far more CPU and memory than they use, so GreenKube
recommends rightsizing them and can turn each recommendation into a pull
request against this repository.

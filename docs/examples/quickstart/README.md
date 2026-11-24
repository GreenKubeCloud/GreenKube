# Quickstart assets

Files used by the [first pull request guide](../../get-started/first-pr.md) and
by the end-to-end harness (`make e2e`, `hack/e2e/`).

| File | Purpose |
|---|---|
| `values.yaml` | GreenKube Helm values with demo-accelerated evidence (30-second samples, 6 samples per recommendation, 6-minute measurement) and a Gitea provider. Not for production. |
| `gitops-repo/` | Content of the sample application repository: `apps/oversized-web` (raw YAML, for Argo CD) and `flux/oversized-worker` (Kustomize, for Flux). Both request far more CPU and memory than they use. |
| `argocd-application.yaml` | Argo CD Application that deploys `apps/oversized-web` into `greenkube-e2e-apps`. |
| `flux-kustomization.yaml` | Flux GitRepository and Kustomization that deploy `flux/oversized-worker` into `greenkube-e2e-apps`. |

The manifests point at `http://gitea-http.gitea.svc.cluster.local:3000/greenkube/greenkube-e2e-quickstart.git`,
an in-cluster Gitea, branch `main`. `hack/e2e/setup-gitops.sh` rewrites the URL
when you override `GITEA_IN_CLUSTER_URL`, `GITEA_OWNER` or `GITEA_REPO` (and the
branch with `GITEA_BRANCH`), and adds the harness's ownership label
`greenkube.io/e2e=true`. The namespace `greenkube-e2e-apps` is fixed: the Argo CD
sample manifests and destination declare it, and the Flux Kustomization's
`targetNamespace` sets it for the namespace-less Flux overlay.

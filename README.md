<img src="https://raw.githubusercontent.com/GreenKubeCloud/GreenKube/refs/heads/gh-pages/assets/greenkube-logo.png" alt="GreenKube Logo" height="80">

# GreenKube

**Every recommendation. One pull request. Proven savings.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://opensource.org/licenses/Apache-2.0) [![Docker Pulls](https://img.shields.io/docker/pulls/greenkube/greenkube)](https://hub.docker.com/r/greenkube/greenkube) [![Build in Public](https://img.shields.io/badge/Build%20in%20Public-blueviolet)](CHANGELOG.md)

Your cluster already has plenty of optimization advice: VPA, Goldilocks, KRR and your cloud console all produce lists, and they often disagree. Nobody wants to resize pods by hand, GitOps reverts anything changed outside Git, and almost no tool tells you what a change really saved once it shipped.

GreenKube turns that advice into reviewed pull requests in your GitOps repository, then measures each merged change against its projection: in money, in CO₂e, and in CPU and memory released.

> **Live demo:** [demo.greenkube.cloud](https://demo.greenkube.cloud), the full dashboard with sample data, no install.

## How it works

1. **Aggregate.** GreenKube's own measurements, corroborated by [VPA](docs/sources/vpa.md), [Goldilocks](docs/sources/goldilocks.md), [Robusta KRR](docs/sources/krr.md) and any tool through a [push API](docs/sources/push.md), in one model.
2. **Decide.** GreenKube's measured value decides each size; the other sources raise or lower the confidence (one vote per engine, contested advice labeled), and each recommendation gets evidence, a projected impact, a risk level, a confidence score and a risk-adjusted ranking.
3. **Deliver.** GreenKube finds the workload's source in Git through Argo CD or Flux (plain YAML, Kustomize, Helm), shows the exact diff, and opens a pull request on GitHub, GitLab or Gitea once you approve the plan.
4. **Prove.** It follows the merge through the sync and the rollout, then measures the change against a counterfactual baseline, with confounders and health gates, and records the outcome in an append-only, hash-chained ledger.

### Released capacity: savings for clusters that do not shrink

On an on-premises cluster or a fixed node pool without an autoscaler, a smaller request does not lower the bill until nodes are removed. GreenKube says so, and reports what you do get: **released capacity**, the CPU cores and memory GiB a change frees. It is projected on each request change and in its pull request, measured from the workload specs after the rollout (verdict `released`, `partially released` or `not released`), and never converted to money or added to cost savings.

## Screenshots

| Overview: measured, pending and projected savings | Recommendations, ranked, with the capacity they free |
|---|---|
| <img src="assets/demo-dashboard.png" width="460"> | <img src="assets/demo-recommendations.png" width="460"> |

| Changes: plan, pull request, sync, rollout, outcome | A measured change: cost interval and released capacity |
|---|---|
| <img src="assets/demo-automation.png" width="460"> | <img src="assets/demo-change.png" width="460"> |

## Quick start

**1. Try the demo (one minute, no cluster).** Sample data, the full UI, nothing to configure:

```bash
docker run --rm -p 9000:9000 greenkube/greenkube demo --no-browser --port 9000   # open http://localhost:9000
```

**2. Install in your cluster.** One Helm release, no profile, no feature flag. Prometheus and OpenCost are discovered when present:

```bash
helm repo add greenkube https://GreenKubeCloud.github.io/GreenKube && helm repo update
helm install greenkube greenkube/greenkube --namespace greenkube --create-namespace

kubectl -n greenkube get secret greenkube-initial-admin -o jsonpath='{.data.password}' | base64 -d; echo
kubectl -n greenkube port-forward svc/greenkube-api 8000:8000   # sign in as admin at http://localhost:8000
```

**3. Let it open pull requests (optional).** One Secret; delivery activates within a minute, no upgrade, no restart:

```bash
kubectl -n greenkube create secret generic greenkube-git-credentials \
  --from-literal=token=<git token> --from-literal=host=github.com
```

The **Setup** page lists what is active and the fix for anything missing. Recommendations need evidence first: most appear after about 3 hours of samples, request decreases after 7 days. To see the whole loop in about 20 minutes on Minikube, with a sample app, Argo CD, Flux and Gitea, follow [Your first pull request](docs/get-started/first-pr.md) (`make e2e`, accelerated values in [`docs/examples/quickstart/values.yaml`](docs/examples/quickstart/values.yaml)).

## Principles

- **Git is the only write path.** No admission webhook, no in-place mutation, no drift for Argo CD or Flux to revert. No pull request is opened without your approval of a plan digest.
- **Projected is never shown as realized.** Projected, measured and billed savings are labeled and never added together; released capacity is never turned into money.
- **On-premises and in the cloud.** Bundled price catalogs for AWS, Azure, OVHcloud and Scaleway, OpenCost or your own rates anywhere, and released capacity where the bill does not move.
- **Fail closed, explain why.** When a change cannot be automated or measured safely, GreenKube stops and tells you what is missing and how to fix it.
- **Neutral.** GreenKube's own analyzers are one source among others; every source is a plugin of the [Source SDK](docs/sources.md).

## Learn more

- [Concepts](docs/concepts.md): the loop and its vocabulary.
- [Installation](docs/installation.md) and [Upgrading](docs/upgrading.md): external PostgreSQL, Ingress/TLS, high availability, upgrades from 0.3.x.
- [GitOps automation](docs/automation.md), [Recommendation lifecycle](docs/recommendation.md), [Recommendation sources](docs/sources.md).
- [Impact model](docs/methodology/impact-model.md) and [Savings measurement](docs/methodology/savings-measurement.md): how cost, CO₂e and released capacity are projected and measured.
- [Configuration](docs/configuration.md), [API](docs/api.md), [CLI](docs/cli.md), [Architecture](docs/architecture.md), [Prometheus & Grafana](docs/prometheus-grafana.md) (operations dashboard and alerts).
- [Roadmap](docs/roadmap.md) and [Changelog](CHANGELOG.md): new features land in the *Unreleased* section before each release.

## Contributing

Contributions are welcome, especially new recommendation sources and support for more GitOps layouts. See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
git clone https://github.com/GreenKubeCloud/GreenKube.git && cd GreenKube
uv sync && uv run pytest
```

## License

[Apache 2.0](LICENSE).

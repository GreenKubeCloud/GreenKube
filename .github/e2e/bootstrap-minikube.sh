#!/usr/bin/env bash
# Prepare a fresh, disposable Minikube cluster for hack/e2e (roadmap O1):
# the prerequisites docs/get-started/first-pr.md lists, at pinned versions.
#
#   metrics-server          minikube addon
#   kube-prometheus-stack   namespace monitoring, 30s scrapes (Prometheus Operator,
#                           kube-state-metrics, kubelet/cAdvisor)
#   OpenCost                namespace opencost, reading that Prometheus: the
#                           verifier only counts OpenCost-priced samples as
#                           trusted, so without it every verdict is inconclusive
#   Argo CD                 namespace argocd
#   Flux                    namespace flux-system
#   Gitea                   namespace gitea (SQLite), admin user "greenkube"
#
# Writes a Gitea API token for the harness to E2E_GITEA_TOKEN_FILE and, on
# GitHub Actions, exports GITEA_TOKEN_FILE, PROMETHEUS_URL and OPENCOST_URL to
# later steps.
#
# It installs cluster-wide components, so it refuses to run outside GitHub
# Actions unless E2E_BOOTSTRAP_ALLOW=1, and always requires the kubectl
# context "minikube". Never point it at a cluster you care about.
set -euo pipefail

: "${E2E_KUBE_CONTEXT:=minikube}"
: "${KPS_VERSION:=92.13.1}"
: "${ARGOCD_VERSION:=v3.3.2}"
: "${FLUX_VERSION:=v2.9.6}"
: "${GITEA_CHART_VERSION:=12.7.0}"
: "${OPENCOST_VERSION:=2.5.33}"
: "${E2E_GITEA_TOKEN_FILE:=${RUNNER_TEMP:-${TMPDIR:-/tmp}}/gitea-token}"
GITEA_ADMIN=greenkube

log() { printf '\033[1;32m[bootstrap]\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[1;31m[bootstrap] ERROR:\033[0m %s\n' "$*" >&2
  exit 1
}

if [[ "${GITHUB_ACTIONS:-}" != "true" && "${E2E_BOOTSTRAP_ALLOW:-0}" != "1" ]]; then
  die "This script installs Prometheus, Argo CD, Flux and Gitea cluster-wide. Run it on a disposable cluster with E2E_BOOTSTRAP_ALLOW=1."
fi
current="$(kubectl config current-context 2>/dev/null || true)"
[[ "${current}" == "${E2E_KUBE_CONTEXT}" ]] || die "kubectl points at '${current:-<none>}', not '${E2E_KUBE_CONTEXT}'."
for cmd in kubectl helm curl jq minikube openssl; do
  command -v "${cmd}" >/dev/null 2>&1 || die "Required command not found: ${cmd}"
done

log "metrics-server addon"
minikube addons enable metrics-server >/dev/null

log "kube-prometheus-stack ${KPS_VERSION}"
helm upgrade --install kps kube-prometheus-stack \
  --repo https://prometheus-community.github.io/helm-charts --version "${KPS_VERSION}" \
  --namespace monitoring --create-namespace \
  --set fullnameOverride=kps \
  --set grafana.enabled=false \
  --set alertmanager.enabled=false \
  --set prometheus.prometheusSpec.scrapeInterval=30s \
  --set prometheus.prometheusSpec.evaluationInterval=30s \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false \
  --wait --timeout 10m >/dev/null

log "OpenCost ${OPENCOST_VERSION}"
helm upgrade --install opencost opencost \
  --repo https://opencost.github.io/opencost-helm-chart --version "${OPENCOST_VERSION}" \
  --namespace opencost --create-namespace \
  --set opencost.prometheus.internal.enabled=true \
  --set opencost.prometheus.internal.serviceName=kps-prometheus \
  --set opencost.prometheus.internal.namespaceName=monitoring \
  --set opencost.prometheus.internal.port=9090 \
  --set opencost.metrics.serviceMonitor.enabled=true \
  --set opencost.ui.enabled=false \
  --wait --timeout 10m >/dev/null

log "Argo CD ${ARGOCD_VERSION}"
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl apply -n argocd --server-side --force-conflicts \
  -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml" >/dev/null

log "Flux ${FLUX_VERSION}"
kubectl apply --server-side --force-conflicts \
  -f "https://github.com/fluxcd/flux2/releases/download/${FLUX_VERSION}/install.yaml" >/dev/null

log "Gitea chart ${GITEA_CHART_VERSION}"
admin_password="$(openssl rand -hex 16)"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "::add-mask::${admin_password}"
fi
values="$(mktemp)"
trap 'rm -f "${values}"' EXIT
cat >"${values}" <<EOF
gitea:
  admin:
    username: ${GITEA_ADMIN}
    password: ${admin_password}
    email: e2e@greenkube.invalid
  config:
    database:
      DB_TYPE: sqlite3
    session:
      PROVIDER: memory
    cache:
      ADAPTER: memory
    queue:
      TYPE: level
    server:
      DOMAIN: gitea-http.gitea.svc.cluster.local
      ROOT_URL: http://gitea-http.gitea.svc.cluster.local:3000/
persistence:
  enabled: true
  size: 2Gi
postgresql:
  enabled: false
postgresql-ha:
  enabled: false
valkey:
  enabled: false
valkey-cluster:
  enabled: false
EOF
helm upgrade --install gitea gitea --repo https://dl.gitea.com/charts --version "${GITEA_CHART_VERSION}" \
  --namespace gitea --create-namespace -f "${values}" --wait --timeout 10m >/dev/null

log "Waiting for Argo CD and Flux"
kubectl -n argocd rollout status deployment/argocd-server --timeout=600s >/dev/null
kubectl -n argocd rollout status deployment/argocd-repo-server --timeout=600s >/dev/null
kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=600s >/dev/null
for controller in source-controller kustomize-controller; do
  kubectl -n flux-system rollout status "deployment/${controller}" --timeout=600s >/dev/null
done

log "Creating the Gitea API token for the harness"
kubectl -n gitea port-forward svc/gitea-http 33001:3000 >/dev/null 2>&1 &
pf_pid=$!
trap 'rm -f "${values}"; kill "${pf_pid}" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:33001/api/v1/version 2>/dev/null && break
  sleep 2
done
# The admin password reaches curl on stdin (--netrc-file), never on its command line.
token="$(printf 'machine 127.0.0.1 login %s password %s\n' "${GITEA_ADMIN}" "${admin_password}" |
  curl -fsS --netrc-file /dev/stdin -X POST -H 'Content-Type: application/json' \
    --data "{\"name\":\"greenkube-e2e-$(date +%s)\",\"scopes\":[\"write:repository\",\"write:user\",\"write:organization\"]}" \
    "http://127.0.0.1:33001/api/v1/users/${GITEA_ADMIN}/tokens" | jq -r .sha1)"
[[ -n "${token}" && "${token}" != "null" ]] || die "Gitea did not return a token."
umask 077
printf '%s' "${token}" >"${E2E_GITEA_TOKEN_FILE}"

prometheus_url="http://kps-prometheus.monitoring.svc.cluster.local:9090"
opencost_url="http://opencost.opencost.svc.cluster.local:9003"
if [[ -n "${GITHUB_ENV:-}" ]]; then
  echo "::add-mask::${token}"
  {
    echo "GITEA_TOKEN_FILE=${E2E_GITEA_TOKEN_FILE}"
    echo "PROMETHEUS_URL=${prometheus_url}"
    echo "OPENCOST_URL=${opencost_url}"
  } >>"${GITHUB_ENV}"
fi
log "Ready: GITEA_TOKEN_FILE=${E2E_GITEA_TOKEN_FILE} PROMETHEUS_URL=${prometheus_url} OPENCOST_URL=${opencost_url}"

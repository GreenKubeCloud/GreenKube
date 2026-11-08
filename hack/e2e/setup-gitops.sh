#!/usr/bin/env bash
# Register the sample repository with the GitOps controllers: one Argo CD
# Application (raw YAML, apps/oversized-web) and, when E2E_FLUX=1, one Flux
# GitRepository + Kustomization (Kustomize, flux/oversized-worker). Creates
# only these objects and the sample namespace. Idempotent (kubectl apply).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd kubectl jq sed

default_repo_url="http://gitea-http.gitea.svc.cluster.local:3000/greenkube/greenkube-e2e-quickstart.git"

render() {
  sed -e "s|${default_repo_url}|${GITEA_REPO_URL}|g" \
    -e "s|targetRevision: main|targetRevision: ${GITEA_BRANCH}|" \
    -e "s|branch: main|branch: ${GITEA_BRANCH}|" "$1"
}

kubectl create namespace "${APPS_NAMESPACE}" --dry-run=client -o yaml |
  kubectl label --local -f - app.kubernetes.io/part-of=greenkube-quickstart -o yaml |
  kubectl apply -f - >/dev/null

log "Applying Argo CD Application ${ARGOCD_NAMESPACE}/${ARGOCD_APP}..."
render "${QUICKSTART_DIR}/argocd-application.yaml" |
  sed -e "s|name: greenkube-e2e-quickstart|name: ${ARGOCD_APP}|" -e "s|namespace: argocd|namespace: ${ARGOCD_NAMESPACE}|" |
  kubectl apply -f - >/dev/null
kubectl -n "${ARGOCD_NAMESPACE}" annotate application "${ARGOCD_APP}" argocd.argoproj.io/refresh=normal --overwrite >/dev/null

argo_synced() {
  local status
  status="$(kubectl -n "${ARGOCD_NAMESPACE}" get application "${ARGOCD_APP}" -o json 2>/dev/null)" || return 1
  [[ "$(jq -r '.status.sync.status' <<<"${status}")" == "Synced" &&
    "$(jq -r '.status.health.status' <<<"${status}")" == "Healthy" ]]
}
wait_until "Argo CD Application ${ARGOCD_APP} Synced/Healthy" 300 5 argo_synced ||
  die "Argo CD Application ${ARGOCD_APP} did not become Synced/Healthy."
argo_revision="$(kubectl -n "${ARGOCD_NAMESPACE}" get application "${ARGOCD_APP}" -o jsonpath='{.status.sync.revision}')"
evidence argocd_app ok "${ARGOCD_NAMESPACE}/${ARGOCD_APP} Synced/Healthy at ${argo_revision}"

if [[ "${E2E_FLUX}" == "1" ]]; then
  if ! kubectl get crd kustomizations.kustomize.toolkit.fluxcd.io >/dev/null 2>&1; then
    warn "Flux CRDs not found; skipping the Flux variant."
    evidence flux_kustomization skipped "Flux is not installed"
    exit 0
  fi
  log "Applying Flux GitRepository + Kustomization ${FLUX_NAMESPACE}/${FLUX_NAME}..."
  render "${QUICKSTART_DIR}/flux-kustomization.yaml" |
    sed -e "s|name: greenkube-e2e-quickstart|name: ${FLUX_NAME}|" -e "s|namespace: flux-system|namespace: ${FLUX_NAMESPACE}|" \
      -e "s|targetNamespace: greenkube-e2e-apps|targetNamespace: ${APPS_NAMESPACE}|" |
    kubectl apply -f - >/dev/null
  kubectl -n "${FLUX_NAMESPACE}" annotate gitrepository "${FLUX_NAME}" \
    reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite >/dev/null

  flux_ready() {
    [[ "$(kubectl -n "${FLUX_NAMESPACE}" get kustomization "${FLUX_NAME}" \
      -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null)" == "True" ]]
  }
  wait_until "Flux Kustomization ${FLUX_NAME} Ready" 300 5 flux_ready ||
    die "Flux Kustomization ${FLUX_NAME} did not become Ready."
  flux_revision="$(kubectl -n "${FLUX_NAMESPACE}" get kustomization "${FLUX_NAME}" -o jsonpath='{.status.lastAppliedRevision}')"
  evidence flux_kustomization ok "${FLUX_NAMESPACE}/${FLUX_NAME} Ready at ${flux_revision}"
fi

kubectl -n "${APPS_NAMESPACE}" rollout status "deployment/${ARGO_WORKLOAD}" --timeout=180s >/dev/null
if [[ "${E2E_FLUX}" == "1" ]]; then
  kubectl -n "${APPS_NAMESPACE}" rollout status "deployment/${FLUX_WORKLOAD}" --timeout=180s >/dev/null
fi
evidence sample_app ok "$(kubectl -n "${APPS_NAMESPACE}" get deploy -o jsonpath='{range .items[*]}{.metadata.name}={.spec.template.spec.containers[0].resources.requests.cpu}/{.spec.template.spec.containers[0].resources.requests.memory} {end}')"

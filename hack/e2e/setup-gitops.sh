#!/usr/bin/env bash
# Register the sample repository with the GitOps controllers: one Argo CD
# Application (raw YAML, apps/oversized-web) and, when E2E_FLUX=1, one Flux
# GitRepository + Kustomization (Kustomize, flux/oversized-worker). E2E_ARGOCD=0
# skips Argo CD (a cluster without it). Creates
# only these objects and the sample namespace, each labelled
# greenkube.io/e2e=true, and refuses to update an existing object without that
# label. Idempotent (kubectl apply).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd kubectl jq sed
require_e2e_context

default_repo_url="http://gitea-http.gitea.svc.cluster.local:3000/greenkube/greenkube-e2e-quickstart.git"
rendered="${E2E_STATE_DIR}/gitops-objects.yaml"

# render <file> <name> <namespace>: point the manifest at this run's repository,
# branch and object names, and add the ownership label.
render() {
  sed -e "s|${default_repo_url}|${GITEA_REPO_URL}|g" \
    -e "s|targetRevision: main|targetRevision: ${GITEA_BRANCH}|" \
    -e "s|branch: main|branch: ${GITEA_BRANCH}|" \
    -e "s|name: greenkube-e2e-quickstart|name: $2|" \
    -e "s|namespace: argocd$|namespace: $3|" -e "s|namespace: flux-system$|namespace: $3|" "$1" >"${rendered}"
  label_owned "${rendered}"
}

ensure_harness_namespace "${APPS_NAMESPACE}"

if [[ "${E2E_ARGOCD}" == "1" ]]; then
  claim_object application "${ARGOCD_APP}" "${ARGOCD_NAMESPACE}"
  log "Applying Argo CD Application ${ARGOCD_NAMESPACE}/${ARGOCD_APP}..."
  render "${QUICKSTART_DIR}/argocd-application.yaml" "${ARGOCD_APP}" "${ARGOCD_NAMESPACE}" |
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
else
  evidence argocd_app skipped "E2E_ARGOCD=0"
fi

if [[ "${E2E_FLUX}" == "1" ]]; then
  if ! kubectl get crd kustomizations.kustomize.toolkit.fluxcd.io >/dev/null 2>&1; then
    warn "Flux CRDs not found; skipping the Flux variant."
    evidence flux_kustomization skipped "Flux is not installed"
    exit 0
  fi
  claim_object gitrepository "${FLUX_NAME}" "${FLUX_NAMESPACE}"
  claim_object kustomization "${FLUX_NAME}" "${FLUX_NAMESPACE}"
  log "Applying Flux GitRepository + Kustomization ${FLUX_NAMESPACE}/${FLUX_NAME}..."
  render "${QUICKSTART_DIR}/flux-kustomization.yaml" "${FLUX_NAME}" "${FLUX_NAMESPACE}" |
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

if [[ "${E2E_ARGOCD}" == "1" ]]; then
  kubectl -n "${APPS_NAMESPACE}" rollout status "deployment/${ARGO_WORKLOAD}" --timeout=180s >/dev/null
fi
if [[ "${E2E_FLUX}" == "1" ]]; then
  kubectl -n "${APPS_NAMESPACE}" rollout status "deployment/${FLUX_WORKLOAD}" --timeout=180s >/dev/null
fi
evidence sample_app ok "$(kubectl -n "${APPS_NAMESPACE}" get deploy -o jsonpath='{range .items[*]}{.metadata.name}={.spec.template.spec.containers[0].resources.requests.cpu}/{.spec.template.spec.containers[0].resources.requests.memory} {end}')"

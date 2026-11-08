#!/usr/bin/env bash
# Remove everything the harness created: the GreenKube release and namespace
# (including its PostgreSQL volume and generated credentials), the sample
# namespace, the harness's Argo CD Application and Flux objects, and the sample
# repository on Gitea. Touches nothing else. Idempotent.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd kubectl helm curl jq
trap stop_port_forwards EXIT

log "Removing Argo CD Application ${ARGOCD_NAMESPACE}/${ARGOCD_APP}..."
kubectl -n "${ARGOCD_NAMESPACE}" delete application "${ARGOCD_APP}" --ignore-not-found --wait=true >/dev/null

if kubectl get crd kustomizations.kustomize.toolkit.fluxcd.io >/dev/null 2>&1; then
  log "Removing Flux objects ${FLUX_NAMESPACE}/${FLUX_NAME}..."
  kubectl -n "${FLUX_NAMESPACE}" delete kustomization "${FLUX_NAME}" --ignore-not-found --wait=true >/dev/null
  kubectl -n "${FLUX_NAMESPACE}" delete gitrepository "${FLUX_NAME}" --ignore-not-found >/dev/null
fi

log "Removing namespace ${APPS_NAMESPACE}..."
kubectl delete namespace "${APPS_NAMESPACE}" --ignore-not-found --wait=true >/dev/null

if helm -n "${GK_NAMESPACE}" status "${GK_RELEASE}" >/dev/null 2>&1; then
  log "Uninstalling Helm release ${GK_NAMESPACE}/${GK_RELEASE}..."
  helm -n "${GK_NAMESPACE}" uninstall "${GK_RELEASE}" --wait >/dev/null
fi
log "Removing namespace ${GK_NAMESPACE} (data and credentials included)..."
kubectl delete namespace "${GK_NAMESPACE}" --ignore-not-found --wait=true >/dev/null
# Cluster-scoped objects of the release (helm uninstall removes them; this
# covers a release whose namespace was deleted first).
kubectl delete clusterrole,clusterrolebinding -l "app.kubernetes.io/instance=${GK_RELEASE}" \
  --field-selector "metadata.name=${GK_RELEASE}" --ignore-not-found >/dev/null 2>&1 || true

gitea_port_forward
if gitea_api GET "/repos/${GITEA_OWNER}/${GITEA_REPO}" >/dev/null 2>&1; then
  log "Deleting Gitea repository ${GITEA_OWNER}/${GITEA_REPO}..."
  gitea_api DELETE "/repos/${GITEA_OWNER}/${GITEA_REPO}" >/dev/null
fi
log "Cleanup complete."

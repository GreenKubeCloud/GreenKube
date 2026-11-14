#!/usr/bin/env bash
# Remove everything the harness created: the GreenKube release and namespace
# (including its PostgreSQL volume and generated credentials), the sample
# namespace, the harness's Argo CD Application and Flux objects, and the sample
# repository on Gitea. Idempotent.
#
# Safety: runs only on the E2E_KUBE_CONTEXT context (default minikube), and
# deletes only objects that carry the harness's ownership marker (label
# greenkube.io/e2e=true; for the Gitea repository, "managed by hack/e2e" in its
# description). Anything else with the same name is reported and left alone,
# unless E2E_ADOPT=1.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd kubectl helm curl jq
require_e2e_context
trap stop_port_forwards EXIT

# PersistentVolumes whose claim lived in the GreenKube namespace, as a JSON array
# (empty when the volumes cannot be listed).
release_volumes() {
  kubectl get pv -o json 2>/dev/null |
    jq -c --arg ns "${GK_NAMESPACE}" '[.items[] | select(.spec.claimRef.namespace == $ns)]'
}

no_release_volumes() {
  local volumes
  volumes="$(release_volumes)"
  [[ -z "${volumes}" || "${volumes}" == "[]" ]]
}

skip_foreign() {
  warn "Leaving $1 alone: it exists but has no ${E2E_OWNER_LABEL} label (set E2E_ADOPT=1 if it is disposable)."
}

if harness_owns application "${ARGOCD_APP}" "${ARGOCD_NAMESPACE}"; then
  log "Removing Argo CD Application ${ARGOCD_NAMESPACE}/${ARGOCD_APP}..."
  kubectl -n "${ARGOCD_NAMESPACE}" delete application "${ARGOCD_APP}" --ignore-not-found --wait=true >/dev/null
elif owner_label application "${ARGOCD_APP}" "${ARGOCD_NAMESPACE}" >/dev/null; then
  skip_foreign "Argo CD Application ${ARGOCD_NAMESPACE}/${ARGOCD_APP}"
fi

if kubectl get crd kustomizations.kustomize.toolkit.fluxcd.io >/dev/null 2>&1; then
  for kind in kustomization gitrepository; do
    if harness_owns "${kind}" "${FLUX_NAME}" "${FLUX_NAMESPACE}"; then
      log "Removing Flux ${kind} ${FLUX_NAMESPACE}/${FLUX_NAME}..."
      kubectl -n "${FLUX_NAMESPACE}" delete "${kind}" "${FLUX_NAME}" --ignore-not-found --wait=true >/dev/null
    elif owner_label "${kind}" "${FLUX_NAME}" "${FLUX_NAMESPACE}" >/dev/null; then
      skip_foreign "Flux ${kind} ${FLUX_NAMESPACE}/${FLUX_NAME}"
    fi
  done
fi

if harness_owns namespace "${APPS_NAMESPACE}"; then
  log "Removing namespace ${APPS_NAMESPACE}..."
  kubectl delete namespace "${APPS_NAMESPACE}" --ignore-not-found --wait=true >/dev/null
elif owner_label namespace "${APPS_NAMESPACE}" >/dev/null; then
  skip_foreign "namespace ${APPS_NAMESPACE}"
fi

if harness_owns namespace "${GK_NAMESPACE}"; then
  if helm -n "${GK_NAMESPACE}" status "${GK_RELEASE}" >/dev/null 2>&1; then
    log "Uninstalling Helm release ${GK_NAMESPACE}/${GK_RELEASE}..."
    helm -n "${GK_NAMESPACE}" uninstall "${GK_RELEASE}" --wait >/dev/null
  fi
  log "Removing namespace ${GK_NAMESPACE} (data and credentials included)..."
  kubectl delete namespace "${GK_NAMESPACE}" --ignore-not-found --wait=true >/dev/null
  # The PostgreSQL volume must be gone before a fresh install: Minikube's hostpath
  # provisioner reuses the directory of a claim name, so a volume it failed to
  # delete would hand the old database (and its old password) to the new release.
  if ! wait_until "the volumes of namespace ${GK_NAMESPACE} to be deleted" "${E2E_TIMEOUT_VOLUMES}" 3 no_release_volumes; then
    while read -r volume path; do
      [[ -n "${volume}" ]] || continue
      if [[ "${path}" == /tmp/hostpath-provisioner/${GK_NAMESPACE}/* ]]; then
        warn "Removing the leftover hostpath volume ${volume} (${path})."
        # </dev/null: minikube ssh reads stdin, which would swallow the rest of
        # the volume list and leave the next volume (the database) in place.
        minikube ssh -- sudo rm -rf "${path}" </dev/null >/dev/null
        kubectl delete pv "${volume}" --ignore-not-found </dev/null >/dev/null
      else
        warn "Volume ${volume} of namespace ${GK_NAMESPACE} was not deleted; delete it before a fresh install."
      fi
    done < <(release_volumes | jq -r '.[] | "\(.metadata.name) \(.spec.hostPath.path // "")"')
  fi
  # Cluster-scoped objects of the release (helm uninstall removes them; this
  # covers a release whose namespace was deleted first).
  kubectl delete clusterrole,clusterrolebinding -l "app.kubernetes.io/instance=${GK_RELEASE}" \
    --field-selector "metadata.name=${GK_RELEASE}" --ignore-not-found >/dev/null 2>&1 || true
elif owner_label namespace "${GK_NAMESPACE}" >/dev/null; then
  skip_foreign "namespace ${GK_NAMESPACE} and Helm release ${GK_RELEASE}"
fi

gitea_port_forward
case "$(gitea_repo_state)" in
  owned)
    log "Deleting Gitea repository ${GITEA_OWNER}/${GITEA_REPO}..."
    gitea_api DELETE "/repos/${GITEA_OWNER}/${GITEA_REPO}" >/dev/null
    ;;
  foreign)
    warn "Leaving Gitea repository ${GITEA_OWNER}/${GITEA_REPO} alone: its description lacks \"${GITEA_REPO_MARKER}\"."
    ;;
esac
log "Cleanup complete."

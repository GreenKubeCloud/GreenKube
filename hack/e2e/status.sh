#!/usr/bin/env bash
# Summarize the last run: loop steps with their evidence, then the live state of
# the GreenKube release, the sample workloads and the GitOps objects.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd kubectl jq

if [[ -s "${E2E_EVIDENCE_DIR}/steps.jsonl" ]]; then
  log "Loop steps (${E2E_EVIDENCE_DIR}/steps.jsonl):"
  jq -r '"  \(.status | ascii_upcase)\t\(.step)\t\(.detail | .[0:220])"' "${E2E_EVIDENCE_DIR}/steps.jsonl"
fi

log "GreenKube (${GK_NAMESPACE}):"
kubectl -n "${GK_NAMESPACE}" get pods 2>/dev/null || true
log "Sample workloads (${APPS_NAMESPACE}):"
kubectl -n "${APPS_NAMESPACE}" get deploy \
  -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,CPU:.spec.template.spec.containers[0].resources.requests.cpu,MEMORY:.spec.template.spec.containers[0].resources.requests.memory' 2>/dev/null || true
log "Argo CD Application ${ARGOCD_NAMESPACE}/${ARGOCD_APP}:"
kubectl -n "${ARGOCD_NAMESPACE}" get application "${ARGOCD_APP}" 2>/dev/null || true
if [[ "${E2E_FLUX}" == "1" ]]; then
  log "Flux Kustomization ${FLUX_NAMESPACE}/${FLUX_NAME}:"
  kubectl -n "${FLUX_NAMESPACE}" get kustomization "${FLUX_NAME}" 2>/dev/null || true
fi

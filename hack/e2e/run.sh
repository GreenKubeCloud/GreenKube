#!/usr/bin/env bash
# End-to-end loop on a live cluster (`make e2e`).
#
#   1. build the image from this checkout inside Minikube   (build-image.sh)
#   2. create and seed the sample repository on Gitea         (setup-repo.sh)
#   3. register it with Argo CD (and Flux)                    (setup-gitops.sh)
#   4. install GreenKube with the quickstart values           (install.sh)
#   5. drive the loop through Argo CD, then Flux              (loop.sh)
#
# E2E_FRESH=1 (default) first removes what a previous run left behind (the
# GreenKube release and its data, the sample repository, the sample namespace
# and the harness's Argo CD/Flux objects), so the run starts from oversized
# workloads and an empty database. E2E_FRESH=0 reuses them.
#
# Safety: nothing runs unless kubectl points at E2E_KUBE_CONTEXT (default
# minikube), and cleanup deletes only objects that carry the harness's
# ownership marker (see cleanup.sh).
#
# The environment is left running for inspection; `make e2e-clean` removes it.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

: "${E2E_FRESH:=1}"
need_cmd kubectl jq
require_e2e_context
need_cmd helm curl minikube docker

trap stop_port_forwards EXIT

: >"${E2E_EVIDENCE_DIR}/steps.jsonl"
log "Evidence directory: ${E2E_EVIDENCE_DIR}"

if [[ "${E2E_FRESH}" == "1" ]]; then
  # cleanup.sh deletes the sample repository: check first that the token can
  # create it again, so a fresh run never leaves the environment without it.
  gitea_port_forward
  require_gitea_recreate_access
  "${E2E_DIR}/cleanup.sh"
  export GITEA_RESET_REPO=1
fi

"${E2E_DIR}/build-image.sh"
"${E2E_DIR}/setup-repo.sh"
"${E2E_DIR}/setup-gitops.sh"
"${E2E_DIR}/install.sh"

status=0
"${E2E_DIR}/loop.sh" argocd || status=1
if [[ "${E2E_FLUX}" == "1" ]]; then
  "${E2E_DIR}/loop.sh" flux || status=1
fi

"${E2E_DIR}/status.sh" || true
if ((status != 0)); then
  die "At least one loop failed; see ${E2E_EVIDENCE_DIR}/steps.jsonl."
fi
log "End-to-end loop passed."

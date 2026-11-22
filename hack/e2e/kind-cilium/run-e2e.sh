#!/usr/bin/env bash
# Drive the hack/e2e harness against the kind + Cilium cluster:
#
#   hack/e2e/kind-cilium/create-cluster.sh
#   hack/e2e/kind-cilium/install-deps.sh
#   GITEA_TOKEN_FILE=... hack/e2e/kind-cilium/run-e2e.sh [build|repo|gitops|install|loop|all]
#
# The harness runs kubectl/helm without --context: it gets a kubeconfig that
# holds only the kind context (KIND_KUBECONFIG), so it can never reach another
# cluster, and E2E_KUBE_CONTEXT makes it refuse anything else. Argo CD is not
# installed on this cluster: only the Flux variant runs. Gitea is the Minikube
# one, reached through the bridge of install-deps.sh (GITEA_LOCAL_PORT is the
# host port-forward, the in-cluster URL is the bridge Service).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../../.." && pwd)"
: "${KIND_CLUSTER:=gk-cilium}"
: "${KIND_STATE_DIR:=${TMPDIR:-/tmp}/greenkube-kind-cilium}"
KIND_STATE_DIR="${KIND_STATE_DIR%/}"
: "${KIND_KUBECONFIG:=${KIND_STATE_DIR}/${KIND_CLUSTER}.kubeconfig}"

export KUBECONFIG="${KIND_KUBECONFIG}"
export E2E_KUBE_CONTEXT="kind-${KIND_CLUSTER}"
export E2E_STATE_DIR="${E2E_STATE_DIR:-${KIND_STATE_DIR}/e2e}"
export E2E_ARGOCD=0
export E2E_FLUX=1
export GK_IMAGE_TAG="${GK_IMAGE_TAG:-e2e-kind}"
export GITEA_LOCAL_PORT="${GITEA_BRIDGE_PORT:-33100}"
# The bridge of install-deps.sh is this run's own Gitea listener.
export E2E_REUSE_PORT_GITEA=1
# Not the harness default (38000): a Minikube e2e on the same host uses it.
export GK_LOCAL_PORT="${GK_LOCAL_PORT:-38100}"
export GITEA_REPO="${GITEA_REPO:-greenkube-e2e-kind}"
# An organization owned by the token's user: creating a repository under the
# user itself (POST /user/repos) needs the write:user scope on Gitea 1.2x,
# creating one in an organization only write:organization + write:repository.
export GITEA_OWNER="${GITEA_OWNER:-greenkube-kind}"
export PROMETHEUS_URL="${PROMETHEUS_URL:-http://kps-prometheus.monitoring.svc.cluster.local:9090}"
export GK_EXTRA_VALUES="${GK_EXTRA_VALUES:-${HERE}/values-kind.yaml}"
export E2E_ALLOW_REGRESSION="${E2E_ALLOW_REGRESSION:-1}"

step="${1:-all}"
if [[ "${step}" == build || "${step}" == all ]]; then
  docker build -t "greenkube/greenkube:${GK_IMAGE_TAG}" "${REPO_ROOT}"
  kind load docker-image "greenkube/greenkube:${GK_IMAGE_TAG}" --name "${KIND_CLUSTER}"
fi
[[ "${step}" == repo || "${step}" == all ]] && "${REPO_ROOT}/hack/e2e/setup-repo.sh"
[[ "${step}" == gitops || "${step}" == all ]] && "${REPO_ROOT}/hack/e2e/setup-gitops.sh"
[[ "${step}" == install || "${step}" == all ]] && "${REPO_ROOT}/hack/e2e/install.sh"
if [[ "${step}" == loop || "${step}" == all ]]; then
  "${REPO_ROOT}/hack/e2e/loop.sh" flux
fi
exit 0

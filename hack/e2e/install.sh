#!/usr/bin/env bash
# Install (or upgrade) GreenKube from ./helm-chart with the quickstart values
# and the image built by build-image.sh. Creates the Git token Secret the
# quickstart provider references. Idempotent (helm upgrade --install).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd kubectl helm jq

kubectl create namespace "${GK_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# The quickstart provider reads key "token" of Secret "greenkube-git".
kubectl -n "${GK_NAMESPACE}" create secret generic greenkube-git \
  --from-literal=token="$(gitea_token)" --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# Cluster-specific overrides: backend URLs and the Gitea repository of this run.
# Helm replaces lists wholesale, so the provider entry is written out in full.
cluster_values="${E2E_STATE_DIR}/values-cluster.yaml"
cat >"${cluster_values}" <<EOF
config:
  prometheus:
    url: ${PROMETHEUS_URL}
  opencost:
    url: ${OPENCOST_URL}
automation:
  providers:
    - id: gitea-quickstart
      type: gitea
      apiBaseUrl: ${GITEA_IN_CLUSTER_URL}/api/v1
      secretName: greenkube-git
      secretKey: token
      repositoryAllowlist:
        - ${GITEA_OWNER}/${GITEA_REPO}
EOF

values=(-f "${QUICKSTART_DIR}/values.yaml" -f "${cluster_values}")
if [[ -n "${GK_EXTRA_VALUES}" ]]; then
  values+=(-f "${GK_EXTRA_VALUES}")
fi

log "helm upgrade --install ${GK_RELEASE} (image ${GK_IMAGE_REPOSITORY}:${GK_IMAGE_TAG})..."
helm upgrade --install "${GK_RELEASE}" "${REPO_ROOT}/helm-chart" \
  --namespace "${GK_NAMESPACE}" \
  "${values[@]}" \
  --set-string image.repository="${GK_IMAGE_REPOSITORY}" \
  --set-string image.tag="${GK_IMAGE_TAG}" \
  --wait --timeout 10m

kubectl -n "${GK_NAMESPACE}" rollout status "deployment/${GK_RELEASE}" --timeout=300s >/dev/null
revision="$(helm -n "${GK_NAMESPACE}" status "${GK_RELEASE}" -o json | jq -r .version)"
pods="$(kubectl -n "${GK_NAMESPACE}" get pods -o jsonpath='{range .items[*]}{.metadata.name}:{.status.phase} {end}')"
evidence install ok "helm revision ${revision}; ${pods}"

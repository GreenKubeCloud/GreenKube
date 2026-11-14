#!/usr/bin/env bash
# Create a kubeadm-based kind cluster (1 control plane + 2 workers) with Cilium
# as the CNI and kube-proxy replacement, L2 announcements + LB IPAM for
# LoadBalancer Services, and Gateway API.
#
#   hack/e2e/kind-cilium/create-cluster.sh
#   hack/e2e/kind-cilium/install-deps.sh
#
# The cluster's kubeconfig goes to its own file (KIND_KUBECONFIG), never to
# ~/.kube/config, so the current context of your shell is left untouched. Use
#   export KUBECONFIG="$(hack/e2e/kind-cilium/create-cluster.sh --print-kubeconfig)"
# or pass --kubeconfig/--context explicitly.
#
# Idempotent: an existing cluster of the same name is reused, Helm releases are
# upgraded in place.
set -euo pipefail

: "${KIND_CLUSTER:=gk-cilium}"
: "${KIND_STATE_DIR:=${TMPDIR:-/tmp}/greenkube-kind-cilium}"
: "${KIND_KUBECONFIG:=${KIND_STATE_DIR%/}/${KIND_CLUSTER}.kubeconfig}"
: "${KIND_NODE_IMAGE:=kindest/node:v1.33.1}"
: "${CILIUM_VERSION:=1.20.3}"
# Cilium 1.20 is built and tested against Gateway API v1.6.x.
: "${GATEWAY_API_VERSION:=v1.6.3}"
# LB IPAM pool: a slice of the kind Docker network that Docker does not hand
# out to containers. Derived from the network's IPv4 subnet unless set.
: "${LB_POOL_START:=}"
: "${LB_POOL_STOP:=}"

if [[ "${1:-}" == "--print-kubeconfig" ]]; then
  printf '%s\n' "${KIND_KUBECONFIG}"
  exit 0
fi

log() { printf '\033[1;32m[kind-cilium]\033[0m %s\n' "$*" >&2; }
for cmd in kind kubectl helm docker jq; do
  command -v "${cmd}" >/dev/null 2>&1 || {
    echo "missing command: ${cmd}" >&2
    exit 1
  }
done

mkdir -p "$(dirname "${KIND_KUBECONFIG}")"
CTX="kind-${KIND_CLUSTER}"
k() { kubectl --kubeconfig "${KIND_KUBECONFIG}" --context "${CTX}" "$@"; }
h() { helm --kubeconfig "${KIND_KUBECONFIG}" --kube-context "${CTX}" "$@"; }

if kind get clusters 2>/dev/null | grep -qx "${KIND_CLUSTER}"; then
  log "Cluster ${KIND_CLUSTER} exists; refreshing its kubeconfig."
  kind get kubeconfig --name "${KIND_CLUSTER}" >"${KIND_KUBECONFIG}"
else
  log "Creating kind cluster ${KIND_CLUSTER} (no default CNI, no kube-proxy)..."
  kind create cluster --name "${KIND_CLUSTER}" --image "${KIND_NODE_IMAGE}" \
    --kubeconfig "${KIND_KUBECONFIG}" --wait 0s --config - <<'EOF'
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
networking:
  disableDefaultCNI: true
  kubeProxyMode: none
nodes:
  - role: control-plane
  - role: worker
  - role: worker
EOF
fi
chmod 600 "${KIND_KUBECONFIG}"

log "Installing Gateway API ${GATEWAY_API_VERSION} CRDs (standard channel)..."
k apply --server-side -f \
  "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml" >/dev/null

log "Installing Cilium ${CILIUM_VERSION}..."
helm repo add cilium https://helm.cilium.io >/dev/null 2>&1 || true
helm repo update cilium >/dev/null
h upgrade --install cilium cilium/cilium --version "${CILIUM_VERSION}" \
  --namespace kube-system \
  --set kubeProxyReplacement=true \
  --set k8sServiceHost="${KIND_CLUSTER}-control-plane" \
  --set k8sServicePort=6443 \
  --set ipam.mode=kubernetes \
  --set operator.replicas=1 \
  --set l2announcements.enabled=true \
  --set externalIPs.enabled=true \
  --set k8sClientRateLimit.qps=20 \
  --set k8sClientRateLimit.burst=40 \
  --set gatewayAPI.enabled=true \
  --set hubble.enabled=false \
  --wait --timeout 10m

k -n kube-system rollout status ds/cilium --timeout=600s
k wait --for=condition=Ready nodes --all --timeout=600s

if [[ -z "${LB_POOL_START}" ]]; then
  subnet="$(docker network inspect kind | jq -r '[.[0].IPAM.Config[].Subnet | select(test("^[0-9.]+/"))][0]')"
  # kind's network is a /16 (for example 172.18.0.0/16): Docker allocates from
  # the bottom, so the top of x.y.255.0/24 is free.
  prefix="$(cut -d. -f1-2 <<<"${subnet}")"
  LB_POOL_START="${prefix}.255.200"
  LB_POOL_STOP="${prefix}.255.250"
fi
log "LB IPAM pool ${LB_POOL_START}-${LB_POOL_STOP}, announced on L2 by every node."
k apply -f - <<EOF
apiVersion: cilium.io/v2
kind: CiliumLoadBalancerIPPool
metadata:
  name: kind-pool
spec:
  blocks:
    - start: ${LB_POOL_START}
      stop: ${LB_POOL_STOP}
---
apiVersion: cilium.io/v2alpha1
kind: CiliumL2AnnouncementPolicy
metadata:
  name: kind-l2
spec:
  loadBalancerIPs: true
  externalIPs: true
  interfaces:
    - ^eth[0-9]+
EOF

log "Done. kubeconfig: ${KIND_KUBECONFIG} (context ${CTX})"

#!/usr/bin/env bash
# Install GreenKube's dependencies on the kind + Cilium cluster created by
# create-cluster.sh, with pinned versions and a small footprint:
#
#   - kube-prometheus-stack (Prometheus Operator, Prometheus, node-exporter,
#     kube-state-metrics; no Alertmanager, no Grafana) in namespace monitoring.
#     Prometheus selects ServiceMonitors and PrometheusRules from every
#     namespace, whatever their labels.
#   - metrics-server (--kubelet-insecure-tls: kind kubelets serve self-signed
#     certificates).
#   - OpenCost, pointed at that Prometheus, in namespace opencost.
#   - Flux source-controller + kustomize-controller (flux CLI; the manifests
#     are the ones embedded in the CLI, i.e. pinned to its version).
#   - A bridge to the Gitea of the Minikube e2e environment (see below).
#
# Gitea bridge: Minikube does not run as a Docker container here (no shared
# Docker network), so the bridge is a host-bound kubectl port-forward on the
# Minikube context (127.0.0.1:GITEA_BRIDGE_PORT) plus, in kind, a selector-less
# Service gitea/gitea-http:3000 whose EndpointSlice points at the Docker
# Desktop host (host.docker.internal) on that port. The in-cluster URL
# http://gitea-http.gitea.svc.cluster.local:3000 then works in kind exactly as
# in Minikube. The port-forward is restarted by re-running this script with
# GITEA_BRIDGE_ONLY=1. It only reads the Minikube cluster.
set -euo pipefail

: "${KIND_CLUSTER:=gk-cilium}"
: "${KIND_STATE_DIR:=${TMPDIR:-/tmp}/greenkube-kind-cilium}"
KIND_STATE_DIR="${KIND_STATE_DIR%/}"
: "${KIND_KUBECONFIG:=${KIND_STATE_DIR}/${KIND_CLUSTER}.kubeconfig}"
: "${KPS_VERSION:=93.2.2}"
: "${METRICS_SERVER_VERSION:=3.14.0}"
: "${OPENCOST_VERSION:=2.5.33}"
: "${GITEA_SOURCE_CONTEXT:=minikube}"
: "${GITEA_BRIDGE_PORT:=33100}"
: "${GITEA_BRIDGE_ONLY:=0}"

log() { printf '\033[1;32m[kind-cilium]\033[0m %s\n' "$*" >&2; }
CTX="kind-${KIND_CLUSTER}"
k() { kubectl --kubeconfig "${KIND_KUBECONFIG}" --context "${CTX}" "$@"; }
h() { helm --kubeconfig "${KIND_KUBECONFIG}" --kube-context "${CTX}" "$@"; }

gitea_bridge() {
  local pid_file="${KIND_STATE_DIR}/gitea-bridge.pid" host_ip
  if ! curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:${GITEA_BRIDGE_PORT}/api/v1/version"; then
    if [[ -s "${pid_file}" ]]; then kill "$(cat "${pid_file}")" 2>/dev/null || true; fi
    log "Port-forwarding ${GITEA_SOURCE_CONTEXT} gitea/gitea-http to 127.0.0.1:${GITEA_BRIDGE_PORT}..."
    nohup kubectl --context "${GITEA_SOURCE_CONTEXT}" -n gitea port-forward svc/gitea-http \
      "${GITEA_BRIDGE_PORT}:3000" >"${KIND_STATE_DIR}/gitea-bridge.log" 2>&1 &
    echo $! >"${pid_file}"
    for _ in $(seq 1 30); do
      curl -fsS -o /dev/null --max-time 3 "http://127.0.0.1:${GITEA_BRIDGE_PORT}/api/v1/version" && break
      sleep 1
    done
  fi
  host_ip="$(docker exec "${KIND_CLUSTER}-control-plane" getent ahostsv4 host.docker.internal | awk 'NR==1{print $1}')"
  [[ -n "${host_ip}" ]] || {
    echo "cannot resolve host.docker.internal from the kind nodes" >&2
    exit 1
  }
  log "Gitea bridge: gitea/gitea-http:3000 -> ${host_ip}:${GITEA_BRIDGE_PORT}"
  k apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: gitea
---
apiVersion: v1
kind: Service
metadata:
  name: gitea-http
  namespace: gitea
spec:
  ports:
    - name: http
      port: 3000
      targetPort: ${GITEA_BRIDGE_PORT}
      protocol: TCP
---
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  name: gitea-http-host
  namespace: gitea
  labels:
    kubernetes.io/service-name: gitea-http
    endpointslice.kubernetes.io/managed-by: greenkube-e2e
addressType: IPv4
ports:
  - name: http
    port: ${GITEA_BRIDGE_PORT}
    protocol: TCP
endpoints:
  - addresses:
      - ${host_ip}
EOF
}

mkdir -p "${KIND_STATE_DIR}"
if [[ "${GITEA_BRIDGE_ONLY}" == "1" ]]; then
  gitea_bridge
  exit 0
fi

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null 2>&1 || true
helm repo add opencost-charts https://opencost.github.io/opencost-helm-chart >/dev/null 2>&1 || true
helm repo update prometheus-community metrics-server opencost-charts >/dev/null

log "kube-prometheus-stack ${KPS_VERSION}..."
h upgrade --install kps prometheus-community/kube-prometheus-stack --version "${KPS_VERSION}" \
  --namespace monitoring --create-namespace \
  --set fullnameOverride=kps \
  --set alertmanager.enabled=false \
  --set grafana.enabled=false \
  --set kubeEtcd.enabled=false \
  --set kubeControllerManager.enabled=false \
  --set kubeScheduler.enabled=false \
  --set kubeProxy.enabled=false \
  --set prometheus.prometheusSpec.retention=2d \
  --set prometheus.prometheusSpec.scrapeInterval=30s \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.probeSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.resources.requests.cpu=200m \
  --set prometheus.prometheusSpec.resources.requests.memory=512Mi \
  --wait --timeout 10m

log "metrics-server ${METRICS_SERVER_VERSION}..."
h upgrade --install metrics-server metrics-server/metrics-server --version "${METRICS_SERVER_VERSION}" \
  --namespace kube-system \
  --set 'args={--kubelet-insecure-tls}' \
  --wait --timeout 5m

log "OpenCost ${OPENCOST_VERSION}..."
h upgrade --install opencost opencost-charts/opencost --version "${OPENCOST_VERSION}" \
  --namespace opencost --create-namespace \
  --set opencost.prometheus.internal.enabled=true \
  --set opencost.prometheus.internal.serviceName=kps-prometheus \
  --set opencost.prometheus.internal.namespaceName=monitoring \
  --set opencost.prometheus.internal.port=9090 \
  --set opencost.metrics.serviceMonitor.enabled=true \
  --set opencost.ui.enabled=false \
  --wait --timeout 10m

log "Flux ($(flux version --client | awk '{print $2}'))..."
flux install --kubeconfig "${KIND_KUBECONFIG}" --context "${CTX}" \
  --components=source-controller,kustomize-controller

gitea_bridge

log "Done. Prometheus: http://kps-prometheus.monitoring.svc.cluster.local:9090, OpenCost: http://opencost.opencost.svc.cluster.local:9003"

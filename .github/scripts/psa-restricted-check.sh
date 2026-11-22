#!/usr/bin/env bash
# Pod Security Admission "restricted" check of the Helm chart (roadmap N5).
#
# Renders ./helm-chart with several value sets and sends every object to the
# API server with `kubectl apply --dry-run=server` into a namespace labelled
# pod-security.kubernetes.io/{enforce,warn}=restricted. Pods are rejected by
# "enforce"; Deployments, StatefulSets, Jobs and CronJobs only produce a
# "would violate PodSecurity" warning, which this script turns into a failure.
# Nothing is persisted except the throwaway namespace, deleted on exit.
#
# Usage: .github/scripts/psa-restricted-check.sh
#   PSA_KUBE_CONTEXT  kubectl context of a disposable cluster (default: the
#                     current context, which must start with "kind-")
#   PSA_NAMESPACE     namespace to create (default: greenkube-psa-check)
#   CHART_DIR         chart directory (default: ./helm-chart)
set -euo pipefail

CHART_DIR="${CHART_DIR:-./helm-chart}"
PSA_NAMESPACE="${PSA_NAMESPACE:-greenkube-psa-check}"
context="${PSA_KUBE_CONTEXT:-$(kubectl config current-context)}"
if [[ -z "${PSA_KUBE_CONTEXT:-}" && "${context}" != kind-* ]]; then
  echo "Refusing to run against context '${context}': set PSA_KUBE_CONTEXT to a disposable cluster." >&2
  exit 2
fi
kc=(kubectl --context "${context}")

work="$(mktemp -d)"
cleanup() {
  local status=$?
  "${kc[@]}" delete namespace "${PSA_NAMESPACE}" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  rm -rf "${work}"
  exit "${status}"
}
trap cleanup EXIT

"${kc[@]}" create namespace "${PSA_NAMESPACE}" --dry-run=client -o yaml |
  "${kc[@]}" label --local -f - \
    pod-security.kubernetes.io/enforce=restricted \
    pod-security.kubernetes.io/enforce-version=latest \
    pod-security.kubernetes.io/warn=restricted \
    pod-security.kubernetes.io/warn-version=latest -o yaml |
  "${kc[@]}" apply -f - >/dev/null

# Each scenario: a name and the extra `helm template` arguments. Objects whose
# CRDs a bare cluster lacks (ServiceMonitor, PrometheusRule, HTTPRoute) are
# left out; their templates hold no pod.
scenarios=(
  "defaults|"
  "no-persistence|--set postgres.persistence.enabled=false"
  "dedicated-worker|--set worker.replicas=1 --set api.replicas=2 --set controller.replicas=2"
  "external-postgres|--set postgres.enabled=false --set secrets.dbConnectionString=postgresql://u:p@db:5432/greenkube"
  "ingress|--set ingress.enabled=true --set ingress.hosts[0]=greenkube.example.com"
  "network-policy|--set monitoring.networkPolicy.enabled=true"
  "postgres-backup|--set postgres.backup.enabled=true"
)

failed=0
for scenario in "${scenarios[@]}"; do
  name="${scenario%%|*}"
  read -r -a extra <<<"${scenario#*|}"
  rendered="${work}/${name}.yaml"
  helm template greenkube "${CHART_DIR}" --namespace "${PSA_NAMESPACE}" ${extra[@]+"${extra[@]}"} >"${rendered}"
  output="${work}/${name}.log"
  if ! "${kc[@]}" -n "${PSA_NAMESPACE}" apply --dry-run=server -f "${rendered}" >"${output}" 2>&1; then
    echo "::error::[${name}] server-side dry run failed" >&2
    cat "${output}" >&2
    failed=1
    continue
  fi
  if grep -q "violate PodSecurity" "${output}"; then
    echo "::error::[${name}] the chart violates Pod Security \"restricted\"" >&2
    grep "violate PodSecurity" "${output}" >&2
    failed=1
    continue
  fi
  pods="$(grep -cE '^(deployment|statefulset|job|cronjob|pod)\.' "${output}" || true)"
  echo "[${name}] ok: ${pods} pod-bearing objects admitted under restricted"
done

exit "${failed}"

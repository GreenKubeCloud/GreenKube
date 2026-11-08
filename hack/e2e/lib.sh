# shellcheck shell=bash
# Shared helpers for the GreenKube end-to-end harness (hack/e2e).
#
# Every script sources this file. All settings are environment variables with
# defaults that match a Minikube cluster where Argo CD (namespace argocd), Flux
# (flux-system), Gitea (gitea) and kube-prometheus (monitoring) are installed.
# Override any of them before running `make e2e`.

set -euo pipefail

E2E_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${E2E_DIR}/../.." && pwd)"
QUICKSTART_DIR="${REPO_ROOT}/docs/examples/quickstart"

# --- GreenKube release --------------------------------------------------------
: "${GK_NAMESPACE:=greenkube}"
: "${GK_RELEASE:=greenkube}"
: "${GK_IMAGE_REPOSITORY:=greenkube/greenkube}"
: "${GK_IMAGE_TAG:=e2e-dev}"
# Set to 1 to reuse an image that is already present in the Minikube runtime.
: "${GK_SKIP_BUILD:=0}"
: "${GK_LOCAL_PORT:=38000}"
# Extra Helm values file merged after the quickstart and cluster values.
: "${GK_EXTRA_VALUES:=}"

# --- Sample application ---------------------------------------------------------
: "${APPS_NAMESPACE:=greenkube-e2e-apps}"
: "${ARGO_WORKLOAD:=oversized-web}"
: "${FLUX_WORKLOAD:=oversized-worker}"

# --- Gitea ----------------------------------------------------------------------
: "${GITEA_NAMESPACE:=gitea}"
: "${GITEA_SERVICE:=gitea-http}"
: "${GITEA_SERVICE_PORT:=3000}"
: "${GITEA_IN_CLUSTER_URL:=http://${GITEA_SERVICE}.${GITEA_NAMESPACE}.svc.cluster.local:${GITEA_SERVICE_PORT}}"
: "${GITEA_LOCAL_PORT:=33000}"
# Browser-facing Gitea origin used in pull-request links (webBaseUrl).
: "${GITEA_WEB_URL:=http://localhost:3000}"
: "${GITEA_OWNER:=greenkube}"
: "${GITEA_REPO:=greenkube-e2e-quickstart}"
: "${GITEA_BRANCH:=main}"
# Token of GITEA_OWNER with read/write repository scope. GITEA_TOKEN wins over the file.
: "${GITEA_TOKEN_FILE:=${HOME}/Documents/Entreprise/GreenKube/local-k8s/.bootstrap-state/gitea-token}"
# Set to 1 to delete and recreate the sample repository (it is owned by the harness).
: "${GITEA_RESET_REPO:=0}"

# --- GitOps controllers -----------------------------------------------------------
: "${ARGOCD_NAMESPACE:=argocd}"
: "${ARGOCD_APP:=greenkube-e2e-quickstart}"
: "${FLUX_NAMESPACE:=flux-system}"
: "${FLUX_NAME:=greenkube-e2e-quickstart}"
# Set to 0 to skip the Flux variant.
: "${E2E_FLUX:=1}"

# --- Observability backends (in-cluster URLs used by GreenKube) -------------------
: "${PROMETHEUS_URL:=http://prometheus-k8s.monitoring.svc.cluster.local:9090}"
: "${OPENCOST_URL:=http://opencost.opencost.svc.cluster.local:9003}"

# --- Timeouts (seconds) -------------------------------------------------------------
: "${E2E_TIMEOUT_RECOMMENDATION:=1200}"
: "${E2E_TIMEOUT_PR:=300}"
: "${E2E_TIMEOUT_SYNC:=600}"
: "${E2E_TIMEOUT_APPLY:=600}"

# --- Local state: port-forward PIDs, logs and the evidence of the last run ---------
: "${E2E_STATE_DIR:=${TMPDIR:-/tmp}/greenkube-e2e}"
E2E_STATE_DIR="${E2E_STATE_DIR%/}"
mkdir -p "${E2E_STATE_DIR}"
E2E_EVIDENCE_DIR="${E2E_STATE_DIR}/evidence"
mkdir -p "${E2E_EVIDENCE_DIR}"

GITEA_LOCAL_URL="http://127.0.0.1:${GITEA_LOCAL_PORT}"
GK_LOCAL_URL="http://127.0.0.1:${GK_LOCAL_PORT}"
GITEA_REPO_URL="${GITEA_IN_CLUSTER_URL}/${GITEA_OWNER}/${GITEA_REPO}.git"

log() { printf '\033[1;32m[e2e]\033[0m %s\n' "$*" >&2; }
warn() { printf '\033[1;33m[e2e] WARN:\033[0m %s\n' "$*" >&2; }
die() {
  printf '\033[1;31m[e2e] ERROR:\033[0m %s\n' "$*" >&2
  exit 1
}

need_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "${cmd}" >/dev/null 2>&1 || die "Required command not found: ${cmd}"
  done
}

# Record one loop step for the final report: evidence <step> <status> <detail>.
evidence() {
  local step="$1" status="$2" detail="${3:-}"
  jq -cn --arg step "${step}" --arg status "${status}" --arg detail "${detail}" \
    --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{at: $at, step: $step, status: $status, detail: $detail}' >>"${E2E_EVIDENCE_DIR}/steps.jsonl"
  log "step ${step}: ${status}${detail:+ — ${detail}}"
}

# Save a JSON document under the evidence directory: save_json <name> <json>.
save_json() {
  printf '%s\n' "$2" >"${E2E_EVIDENCE_DIR}/$1.json"
}

# wait_until <description> <timeout-seconds> <interval-seconds> <command...>
# Runs the command until it succeeds or the timeout elapses.
wait_until() {
  local description="$1" timeout="$2" interval="$3"
  shift 3
  local deadline=$((SECONDS + timeout))
  until "$@"; do
    if ((SECONDS >= deadline)); then
      warn "Timed out after ${timeout}s waiting for: ${description}"
      return 1
    fi
    sleep "${interval}"
  done
}

gitea_token() {
  if [[ -n "${GITEA_TOKEN:-}" ]]; then
    printf '%s' "${GITEA_TOKEN}"
    return
  fi
  [[ -s "${GITEA_TOKEN_FILE}" ]] || die "Set GITEA_TOKEN or GITEA_TOKEN_FILE (missing: ${GITEA_TOKEN_FILE})."
  tr -d '[:space:]' <"${GITEA_TOKEN_FILE}"
}

# --- Port-forwards ------------------------------------------------------------------
# port_forward <name> <namespace> <resource> <local-port> <remote-port> <probe-url>
# Starts (or reuses) a kubectl port-forward and waits until the probe URL answers.
port_forward() {
  local name="$1" namespace="$2" resource="$3" local_port="$4" remote_port="$5" probe="$6"
  local pid_file="${E2E_STATE_DIR}/pf-${name}.pid"
  if curl -fsS -o /dev/null --max-time 3 "${probe}" 2>/dev/null; then
    return 0
  fi
  if [[ -s "${pid_file}" ]]; then
    kill "$(cat "${pid_file}")" >/dev/null 2>&1 || true
    rm -f "${pid_file}"
  fi
  kubectl -n "${namespace}" port-forward "${resource}" "${local_port}:${remote_port}" \
    >"${E2E_STATE_DIR}/pf-${name}.log" 2>&1 &
  echo $! >"${pid_file}"
  local attempt
  for attempt in $(seq 1 60); do
    if curl -fsS -o /dev/null --max-time 3 "${probe}" 2>/dev/null; then
      return 0
    fi
    if ! kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
      # The port-forward exits when its pod restarts; start it again.
      kubectl -n "${namespace}" port-forward "${resource}" "${local_port}:${remote_port}" \
        >"${E2E_STATE_DIR}/pf-${name}.log" 2>&1 &
      echo $! >"${pid_file}"
    fi
    sleep 1
  done
  die "Port-forward ${name} (${namespace}/${resource}) did not answer on ${probe}."
}

stop_port_forwards() {
  local pid_file
  for pid_file in "${E2E_STATE_DIR}"/pf-*.pid; do
    [[ -e "${pid_file}" ]] || continue
    kill "$(cat "${pid_file}")" >/dev/null 2>&1 || true
    rm -f "${pid_file}"
  done
}

gitea_port_forward() {
  port_forward gitea "${GITEA_NAMESPACE}" "svc/${GITEA_SERVICE}" "${GITEA_LOCAL_PORT}" "${GITEA_SERVICE_PORT}" \
    "${GITEA_LOCAL_URL}/api/v1/version"
}

greenkube_port_forward() {
  port_forward greenkube "${GK_NAMESPACE}" "svc/${GK_RELEASE}-api" "${GK_LOCAL_PORT}" 8000 \
    "${GK_LOCAL_URL}/api/v1/health/heartbeat"
}

# --- HTTP helpers --------------------------------------------------------------------
# gitea_api <method> <path> [json-body]; prints the response body, fails on HTTP errors.
gitea_api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "${method}" -H "Authorization: token $(gitea_token)" -H "Accept: application/json")
  if [[ -n "${body}" ]]; then
    args+=(-H "Content-Type: application/json" --data "${body}")
  fi
  curl --fail-with-body "${args[@]}" "${GITEA_LOCAL_URL}/api/v1${path}"
}

greenkube_api_key() {
  if [[ -n "${GREENKUBE_API_KEY:-}" ]]; then
    printf '%s' "${GREENKUBE_API_KEY}"
    return
  fi
  kubectl -n "${GK_NAMESPACE}" get secret "${GK_RELEASE}" -o jsonpath='{.data.GREENKUBE_API_KEY}' | base64 -d
}

# gk_api <method> <path> [json-body]; path is relative to /api/v1. Prints the body and
# fails on HTTP errors; the body of a failed call is kept in ${E2E_STATE_DIR}/last-error.json.
gk_api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "${method}" -H "Authorization: Bearer ${GK_API_KEY:?GK_API_KEY is not set}" -H "Accept: application/json")
  if [[ -n "${body}" ]]; then
    args+=(-H "Content-Type: application/json" --data "${body}")
  fi
  local output status
  output="$(curl "${args[@]}" -w '\n%{http_code}' "${GK_LOCAL_URL}/api/v1${path}")" || return 1
  status="${output##*$'\n'}"
  output="${output%$'\n'*}"
  if [[ "${status}" -ge 400 ]]; then
    printf '%s\n' "${output}" >"${E2E_STATE_DIR}/last-error.json"
    warn "${method} /api/v1${path} -> HTTP ${status}: ${output:0:600}"
    return 1
  fi
  printf '%s' "${output}"
}

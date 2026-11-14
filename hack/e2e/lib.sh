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

# --- Safety ----------------------------------------------------------------------
# Every script that changes the cluster first checks that kubectl points at this
# context, so a shell left on a real cluster is never touched.
: "${E2E_KUBE_CONTEXT:=minikube}"
# Ownership marker: the harness labels the namespaces, the Argo CD Application
# and the Flux objects it creates, and only deletes or reuses labelled objects.
# The sample repository carries GITEA_REPO_MARKER in its description instead.
E2E_OWNER_LABEL_KEY="greenkube.io/e2e"
E2E_OWNER_LABEL="${E2E_OWNER_LABEL_KEY}=true"
GITEA_REPO_MARKER="managed by hack/e2e"
# Set to 1 to take over objects that have the harness's names but no ownership
# marker (for example an environment created before the marker existed). They
# are labelled and reused and, with E2E_FRESH=1, deleted.
: "${E2E_ADOPT:=0}"

# --- Sample application ---------------------------------------------------------
# Fixed: the sample manifests, the Argo CD destination and the Flux overlay all
# declare this namespace.
APPS_NAMESPACE_FIXED="greenkube-e2e-apps"
: "${APPS_NAMESPACE:=${APPS_NAMESPACE_FIXED}}"
if [[ "${APPS_NAMESPACE}" != "${APPS_NAMESPACE_FIXED}" ]]; then
  printf '[e2e] ERROR: APPS_NAMESPACE cannot be changed: the sample repository deploys into %s.\n' \
    "${APPS_NAMESPACE_FIXED}" >&2
  exit 1
fi
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
# Token with write:repository scope: GITEA_TOKEN, or a file that holds it in
# GITEA_TOKEN_FILE (GITEA_TOKEN wins). The token's user must be GITEA_OWNER,
# a member of the GITEA_OWNER organization, or a Gitea administrator. To create
# the sample repository Gitea also wants write:user when GITEA_OWNER is the
# token's own user (POST /user/repos), write:organization when it is an
# organization, write:admin otherwise.
: "${GITEA_TOKEN_FILE:=}"
# Set to 1 to delete and recreate the sample repository (only when its
# description carries the harness marker, see GITEA_REPO_MARKER).
: "${GITEA_RESET_REPO:=0}"

# --- GitOps controllers -----------------------------------------------------------
: "${ARGOCD_NAMESPACE:=argocd}"
: "${ARGOCD_APP:=greenkube-e2e-quickstart}"
: "${FLUX_NAMESPACE:=flux-system}"
: "${FLUX_NAME:=greenkube-e2e-quickstart}"
# Set to 0 to skip the Flux variant.
: "${E2E_FLUX:=1}"
# Set to 0 to skip the Argo CD variant (a cluster without Argo CD).
: "${E2E_ARGOCD:=1}"

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
  [[ -n "${GITEA_TOKEN_FILE}" ]] || die "Set GITEA_TOKEN, or GITEA_TOKEN_FILE to a file that holds the Gitea token."
  [[ -s "${GITEA_TOKEN_FILE}" ]] || die "GITEA_TOKEN_FILE is empty or missing: ${GITEA_TOKEN_FILE}"
  tr -d '[:space:]' <"${GITEA_TOKEN_FILE}"
}

# --- Safety checks -----------------------------------------------------------------
# Refuse to run against any kubectl context other than E2E_KUBE_CONTEXT.
require_e2e_context() {
  local current
  current="$(kubectl config current-context 2>/dev/null || true)"
  [[ "${current}" == "${E2E_KUBE_CONTEXT}" ]] ||
    die "kubectl points at context '${current:-<none>}', not '${E2E_KUBE_CONTEXT}'. The harness installs, upgrades and deletes objects: switch context (kubectl config use-context ${E2E_KUBE_CONTEXT}) or set E2E_KUBE_CONTEXT to the context of a disposable cluster."
}

# owner_label <kind> <name> [namespace]: prints the value of the harness label
# of an object ("" when unlabelled); fails when the object does not exist.
owner_label() {
  local kind="$1" name="$2" namespace="${3:-}" object
  if [[ -n "${namespace}" ]]; then
    object="$(kubectl -n "${namespace}" get "${kind}" "${name}" -o json 2>/dev/null)" || return 1
  else
    object="$(kubectl get "${kind}" "${name}" -o json 2>/dev/null)" || return 1
  fi
  jq -r --arg key "${E2E_OWNER_LABEL_KEY}" '.metadata.labels[$key] // ""' <<<"${object}"
}

# harness_owns <kind> <name> [namespace]: true when the object exists and
# belongs to the harness (labelled, or E2E_ADOPT=1); false when it is missing.
harness_owns() {
  local value
  value="$(owner_label "$@")" || return 1
  [[ "${value}" == "true" || "${E2E_ADOPT}" == "1" ]]
}

# claim_object <kind> <name> [namespace]: called before the harness creates or
# updates an object. Passes when the object is missing or belongs to the
# harness; dies on an object that someone else created.
claim_object() {
  local kind="$1" name="$2" namespace="${3:-}" value
  value="$(owner_label "$@")" || return 0
  [[ "${value}" == "true" ]] && return 0
  if [[ "${E2E_ADOPT}" == "1" ]]; then
    warn "Adopting ${kind} ${namespace:+${namespace}/}${name} (E2E_ADOPT=1)."
    return 0
  fi
  die "${kind} ${namespace:+${namespace}/}${name} exists but was not created by the harness (no label ${E2E_OWNER_LABEL}). Choose another name, or set E2E_ADOPT=1 if it is disposable."
}

# ensure_harness_namespace <name>: create the namespace with the ownership
# label, or check that the existing namespace belongs to the harness.
ensure_harness_namespace() {
  local namespace="$1"
  claim_object namespace "${namespace}"
  kubectl create namespace "${namespace}" --dry-run=client -o yaml |
    kubectl label --local -f - "${E2E_OWNER_LABEL}" app.kubernetes.io/part-of=greenkube-quickstart -o yaml |
    kubectl apply -f - >/dev/null
}

# label_owned <file>: add the ownership label to every object of a manifest
# (read from the file) and print them as one List. `kubectl label --local -o
# yaml` would print several objects without "---" separators, so the objects
# are collected as JSON instead.
label_owned() {
  kubectl label --local -f "$1" "${E2E_OWNER_LABEL}" -o json |
    jq -s '{apiVersion: "v1", kind: "List", items: .}'
}

# --- Port-forwards ------------------------------------------------------------------
# port_forward <name> <namespace> <resource> <local-port> <remote-port> <probe-url>
# Starts (or reuses) a kubectl port-forward and waits until the probe URL answers.
port_forward() {
  local name="$1" namespace="$2" resource="$3" local_port="$4" remote_port="$5" probe="$6"
  local pid_file="${E2E_STATE_DIR}/pf-${name}.pid"
  if curl -fsS -o /dev/null --max-time 3 "${probe}" 2>/dev/null; then
    # Reuse only this harness's own port-forward: a listener started by anyone
    # else (another e2e run on another cluster) would silently drive the wrong
    # cluster. E2E_REUSE_PORT_<NAME>=1 accepts a listener you started yourself.
    local reuse_var="E2E_REUSE_PORT_$(printf '%s' "${name}" | tr '[:lower:]-' '[:upper:]_')"
    if [[ -s "${pid_file}" ]] && kill -0 "$(cat "${pid_file}")" 2>/dev/null; then
      return 0
    fi
    [[ "${!reuse_var:-0}" == "1" ]] && return 0
    die "Port ${local_port} (${name}) already answers but was not opened by this harness (no live ${pid_file}): another port-forward, maybe to another cluster, holds it. Pick a free port (GK_LOCAL_PORT / GITEA_LOCAL_PORT), or set ${reuse_var}=1 if that listener is yours."
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
# Credentials reach curl on stdin (-H @-), never on its command line.
gitea_api() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "${method}" -H @- -H "Accept: application/json")
  if [[ -n "${body}" ]]; then
    args+=(-H "Content-Type: application/json" --data "${body}")
  fi
  printf 'Authorization: token %s\n' "$(gitea_token)" |
    curl --fail-with-body "${args[@]}" "${GITEA_LOCAL_URL}/api/v1${path}"
}

# gitea_call <method> <path> [json-body]: like gitea_api, but never fails on an
# HTTP error. Prints the HTTP status code (000 when Gitea is unreachable); the
# response body goes to ${E2E_STATE_DIR}/gitea-last.json.
gitea_call() {
  local method="$1" path="$2" body="${3:-}"
  local args=(-sS -X "${method}" -H @- -H "Accept: application/json"
    -o "${E2E_STATE_DIR}/gitea-last.json" -w '%{http_code}')
  if [[ -n "${body}" ]]; then
    args+=(-H "Content-Type: application/json" --data "${body}")
  fi
  printf 'Authorization: token %s\n' "$(gitea_token)" |
    curl "${args[@]}" "${GITEA_LOCAL_URL}/api/v1${path}" 2>/dev/null || printf '000'
}

# gitea_repo_state: prints "missing", "owned" (the description carries the
# harness marker, or E2E_ADOPT=1) or "foreign".
gitea_repo_state() {
  local repo
  if ! repo="$(gitea_api GET "/repos/${GITEA_OWNER}/${GITEA_REPO}" 2>/dev/null)"; then
    printf 'missing'
  elif [[ "$(jq -r '.description // ""' <<<"${repo}")" == *"${GITEA_REPO_MARKER}"* || "${E2E_ADOPT}" == "1" ]]; then
    printf 'owned'
  else
    printf 'foreign'
  fi
}

# The API key install.sh passes as secrets.apiKey: GREENKUBE_API_KEY when set, else
# the key saved in the state directory, else a key an earlier chart generated into the
# release Secret, else a new random one. Saved (mode 600) so upgrades keep it.
harness_api_key() {
  local file="${E2E_STATE_DIR}/api-key" key=""
  if [[ -n "${GREENKUBE_API_KEY:-}" ]]; then
    printf '%s' "${GREENKUBE_API_KEY}"
    return
  fi
  if [[ ! -s "${file}" ]]; then
    key="$(kubectl -n "${GK_NAMESPACE}" get secret "${GK_RELEASE}" -o jsonpath='{.data.GREENKUBE_API_KEY}' 2>/dev/null |
      base64 -d 2>/dev/null || true)"
    if [[ -z "${key}" ]]; then
      key="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48 || true)"
    fi
    (umask 077 && printf '%s' "${key}" >"${file}")
  fi
  cat "${file}"
}

greenkube_api_key() {
  if [[ -n "${GREENKUBE_API_KEY:-}" ]]; then
    printf '%s' "${GREENKUBE_API_KEY}"
    return
  fi
  if [[ -s "${E2E_STATE_DIR}/api-key" ]]; then
    cat "${E2E_STATE_DIR}/api-key"
    return
  fi
  # Supplied in values (secrets.apiKey) or generated by an earlier chart.
  local secret key
  for secret in "${GK_RELEASE}-values" "${GK_RELEASE}"; do
    key="$(kubectl -n "${GK_NAMESPACE}" get secret "${secret}" -o jsonpath='{.data.GREENKUBE_API_KEY}' 2>/dev/null |
      base64 -d 2>/dev/null || true)"
    if [[ -n "${key}" ]]; then
      printf '%s' "${key}"
      return
    fi
  done
}

# gk_api <method> <path> [json-body]; path is relative to /api/v1 unless it starts
# with /api/ (e.g. /api/v2/changes). Prints the body and fails on HTTP errors; the
# body of a failed call is kept in ${E2E_STATE_DIR}/last-error.json.
gk_api() {
  local method="$1" path="$2" body="${3:-}"
  [[ "${path}" == /api/* ]] || path="/api/v1${path}"
  local args=(-sS -X "${method}" -H @- -H "Accept: application/json")
  if [[ -n "${body}" ]]; then
    args+=(-H "Content-Type: application/json" --data "${body}")
  fi
  local output status
  output="$(printf 'Authorization: Bearer %s\n' "${GK_API_KEY:?GK_API_KEY is not set}" |
    curl "${args[@]}" -w '\n%{http_code}' "${GK_LOCAL_URL}${path}")" || return 1
  status="${output##*$'\n'}"
  output="${output%$'\n'*}"
  if [[ "${status}" -ge 400 ]]; then
    printf '%s\n' "${output}" >"${E2E_STATE_DIR}/last-error.json"
    warn "${method} ${path} -> HTTP ${status}: ${output:0:600}"
    return 1
  fi
  printf '%s' "${output}"
}

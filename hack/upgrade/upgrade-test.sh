#!/usr/bin/env bash
# Real upgrade test from GreenKube 0.3.1 to this working tree, on kind (roadmap F17).
#
#   1. install the 0.3.1 chart (from the v0.3.1 git tag, or the published chart)
#      with the 0.3.1 image and its bundled PostgreSQL;
#   2. seed it (hack/upgrade/seed-v0.3.1.sql) and record the row counts
#      (hack/upgrade/counts.sql), the schema version and the API key;
#   3. helm upgrade --reset-then-reuse-values --wait to ./helm-chart with the
#      image built from this checkout;
#   4. assert: api and controller Ready, schema at the latest shipped migration,
#      every seeded row kept, API key unchanged and still accepted;
#   5. roll back as docs/operations/backup-restore.md says: stop the writers,
#      restore the dump taken before the upgrade into an emptied schema, helm
#      rollback; assert the 0.3.1 schema version, the seeded rows and a Ready
#      0.3.1 release (UPGRADE_ROLLBACK=0 skips it).
#
# It only ever talks to a kind cluster, through its own kubeconfig file: it never
# reads or changes the current kubectl context (a shared Minikube stays untouched).
#
# Usage: hack/upgrade/upgrade-test.sh
#   UPGRADE_CLUSTER      kind cluster name (default greenkube-upgrade); created when
#                        missing and deleted at the end when this run created it
#   UPGRADE_KEEP         1 keeps the cluster and the release for inspection
#   UPGRADE_IMAGE        image of the working tree (default greenkube/greenkube:upgrade-test);
#                        built from this checkout unless UPGRADE_SKIP_BUILD=1
#   UPGRADE_FROM_TAG     released version to upgrade from (default v0.3.1)
#   UPGRADE_STATE_DIR    kubeconfig, logs and evidence (default $TMPDIR/greenkube-upgrade-test)
#   UPGRADE_TIMEOUT      helm --timeout of install and upgrade (default 15m)
#   UPGRADE_ROLLBACK     1 (default) runs the backup-restore rollback phase
set -euo pipefail

UPGRADE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${UPGRADE_DIR}/../.." && pwd)"
: "${UPGRADE_CLUSTER:=greenkube-upgrade}"
: "${UPGRADE_KEEP:=0}"
: "${UPGRADE_IMAGE:=greenkube/greenkube:upgrade-test}"
: "${UPGRADE_SKIP_BUILD:=0}"
: "${UPGRADE_FROM_TAG:=v0.3.1}"
: "${UPGRADE_STATE_DIR:=${TMPDIR:-/tmp}/greenkube-upgrade-test}"
: "${UPGRADE_TIMEOUT:=15m}"
: "${UPGRADE_CHART_REPO:=https://GreenKubeCloud.github.io/GreenKube}"
: "${UPGRADE_ROLLBACK:=1}"
NAMESPACE="greenkube"
RELEASE="greenkube"
FROM_VERSION="${UPGRADE_FROM_TAG#v}"

log() { printf '[upgrade] %s\n' "$*" >&2; }
die() {
  printf '[upgrade] ERROR: %s\n' "$*" >&2
  exit 1
}
need_cmd() {
  local cmd
  for cmd in "$@"; do
    command -v "${cmd}" >/dev/null 2>&1 || die "missing command: ${cmd}"
  done
}

need_cmd kind kubectl helm docker jq git curl
helm_version="$(helm version --template '{{.Version}}')"
case "${helm_version}" in
  v3.1[4-9].* | v3.[2-9][0-9].* | v[4-9].*) ;;
  *) die "helm ${helm_version} lacks --reset-then-reuse-values (Helm 3.14 or later)" ;;
esac

mkdir -p "${UPGRADE_STATE_DIR}/evidence"
EVIDENCE="${UPGRADE_STATE_DIR}/evidence"
: >"${EVIDENCE}/steps.jsonl"
# The cluster's own kubeconfig: the user's current context is never used.
export KUBECONFIG="${UPGRADE_STATE_DIR}/kubeconfig"
CONTEXT="kind-${UPGRADE_CLUSTER}"
kc() { kubectl --context "${CONTEXT}" "$@"; }
hc() { helm --kube-context "${CONTEXT}" "$@"; }

evidence() {
  jq -cn --arg step "$1" --arg status "$2" --arg detail "${3:-}" --arg at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{at: $at, step: $step, status: $status, detail: $detail}' >>"${EVIDENCE}/steps.jsonl"
  log "step $1: $2${3:+ — $3}"
}
fail() {
  evidence "$1" failed "$2"
  exit 1
}

created_cluster=0
port_forward_pid=""
work="$(mktemp -d)"
cleanup() {
  local status=$?
  [[ -n "${port_forward_pid}" ]] && kill "${port_forward_pid}" >/dev/null 2>&1 || true
  if [[ "${status}" -ne 0 ]]; then
    log "Collecting diagnostics into ${EVIDENCE}/cluster"
    mkdir -p "${EVIDENCE}/cluster"
    kc get pods,jobs,pvc -n "${NAMESPACE}" -o wide >"${EVIDENCE}/cluster/resources.txt" 2>&1 || true
    kc get events -n "${NAMESPACE}" --sort-by=.lastTimestamp >"${EVIDENCE}/cluster/events.txt" 2>&1 || true
    for pod in $(kc -n "${NAMESPACE}" get pods -o name 2>/dev/null); do
      kc -n "${NAMESPACE}" logs "${pod}" --all-containers --timestamps \
        >"${EVIDENCE}/cluster/${pod#pod/}.log" 2>&1 || true
      kc -n "${NAMESPACE}" logs "${pod}" --all-containers --timestamps --previous \
        >"${EVIDENCE}/cluster/${pod#pod/}.previous.log" 2>/dev/null || true
    done
  fi
  if [[ "${created_cluster}" == "1" && "${UPGRADE_KEEP}" != "1" ]]; then
    kind delete cluster --name "${UPGRADE_CLUSTER}" --kubeconfig "${KUBECONFIG}" >/dev/null 2>&1 || true
  fi
  rm -rf "${work}"
  exit "${status}"
}
trap cleanup EXIT

# --- Cluster ----------------------------------------------------------------------
if kind get clusters 2>/dev/null | grep -qx "${UPGRADE_CLUSTER}"; then
  kind export kubeconfig --name "${UPGRADE_CLUSTER}" --kubeconfig "${KUBECONFIG}" >/dev/null
  log "Reusing kind cluster ${UPGRADE_CLUSTER}."
else
  log "Creating kind cluster ${UPGRADE_CLUSTER}..."
  kind create cluster --name "${UPGRADE_CLUSTER}" --kubeconfig "${KUBECONFIG}" --wait 120s
  created_cluster=1
fi
[[ "$(kubectl config current-context)" == "${CONTEXT}" ]] || die "unexpected context in ${KUBECONFIG}"
if kc get namespace "${NAMESPACE}" >/dev/null 2>&1; then
  die "namespace ${NAMESPACE} already exists in ${CONTEXT}: delete it or use another UPGRADE_CLUSTER"
fi

# --- Images -----------------------------------------------------------------------
if [[ "${UPGRADE_SKIP_BUILD}" != "1" ]]; then
  log "Building ${UPGRADE_IMAGE} from ${REPO_ROOT}..."
  docker build -t "${UPGRADE_IMAGE}" "${REPO_ROOT}"
fi
kind load docker-image "${UPGRADE_IMAGE}" --name "${UPGRADE_CLUSTER}"
evidence build ok "${UPGRADE_IMAGE} (commit $(git -C "${REPO_ROOT}" rev-parse --short HEAD))"

# --- 1. Install the released chart ---------------------------------------------------
old_chart="${work}/old/helm-chart"
if git -C "${REPO_ROOT}" rev-parse -q --verify "refs/tags/${UPGRADE_FROM_TAG}" >/dev/null; then
  mkdir -p "${work}/old"
  git -C "${REPO_ROOT}" archive "${UPGRADE_FROM_TAG}" helm-chart | tar -x -C "${work}/old"
  source_desc="git tag ${UPGRADE_FROM_TAG}"
else
  hc pull greenkube --repo "${UPGRADE_CHART_REPO}" --version "${FROM_VERSION}" --untar --untardir "${work}/old"
  mv "${work}/old/greenkube" "${old_chart}"
  source_desc="${UPGRADE_CHART_REPO} ${FROM_VERSION}"
fi
log "Installing GreenKube ${FROM_VERSION} (${source_desc})..."
hc install "${RELEASE}" "${old_chart}" --namespace "${NAMESPACE}" --create-namespace \
  --set image.tag="${FROM_VERSION}" --wait --timeout "${UPGRADE_TIMEOUT}" >"${EVIDENCE}/install.txt" ||
  fail install "helm install of ${FROM_VERSION} failed (see install.txt)"
evidence install ok "${RELEASE} ${FROM_VERSION} from ${source_desc}"

# psql inside the bundled PostgreSQL pod (credentials from the pod's own environment).
psql_exec() {
  kc -n "${NAMESPACE}" exec -i "${RELEASE}-postgres-0" -c postgres -- sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -At -F "|" "$@"' psql "$@"
}
schema_version() { psql_exec -c "SELECT COALESCE(max(version), 0) FROM schema_migrations" 2>/dev/null || echo 0; }

waited=0
until [[ "$(schema_version)" -ge 37 ]]; do
  ((waited += 5))
  ((waited < 600)) || fail old_schema "the ${FROM_VERSION} release did not migrate its schema in 10 minutes"
  sleep 5
done
old_version="$(schema_version)"
evidence old_schema ok "schema version ${old_version}"

# --- 2. Seed and record --------------------------------------------------------------
psql_exec -f - <"${UPGRADE_DIR}/seed-v0.3.1.sql" >/dev/null || fail seed "seed-v0.3.1.sql failed"
psql_exec -f - <"${UPGRADE_DIR}/counts.sql" >"${EVIDENCE}/counts-before.txt"
api_key_before="$(kc -n "${NAMESPACE}" get secret "${RELEASE}" -o jsonpath='{.data.GREENKUBE_API_KEY}')"
[[ -n "${api_key_before}" ]] || fail seed "no GREENKUBE_API_KEY in Secret ${RELEASE}"
evidence seed ok "$(tr '\n' ' ' <"${EVIDENCE}/counts-before.txt")"

# The mandatory pre-upgrade backup (docs/upgrading.md): the GreenKube schema, custom format.
kc -n "${NAMESPACE}" exec "${RELEASE}-postgres-0" -c postgres -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema=public --format=custom --no-owner' \
  >"${work}/before-upgrade.dump" || fail backup "pg_dump of the ${FROM_VERSION} schema failed"
[[ -s "${work}/before-upgrade.dump" ]] || fail backup "empty pre-upgrade dump"
evidence backup ok "$(wc -c <"${work}/before-upgrade.dump" | tr -d ' ') bytes"

# --- 3. Upgrade ------------------------------------------------------------------------
log "Upgrading to the working tree (${UPGRADE_IMAGE})..."
hc upgrade "${RELEASE}" "${REPO_ROOT}/helm-chart" --namespace "${NAMESPACE}" --reset-then-reuse-values \
  --set image.repository="${UPGRADE_IMAGE%:*}" --set image.tag="${UPGRADE_IMAGE##*:}" \
  --set image.pullPolicy=IfNotPresent \
  --wait --timeout "${UPGRADE_TIMEOUT}" >"${EVIDENCE}/upgrade.txt" 2>&1 ||
  fail upgrade "helm upgrade failed: $(tail -n 5 "${EVIDENCE}/upgrade.txt" | tr '\n' ' ')"
evidence upgrade ok "$(hc status "${RELEASE}" -n "${NAMESPACE}" -o json | jq -c '{revision: .version, status: .info.status}')"

# --- 4. Assertions -----------------------------------------------------------------------
for component in api controller; do
  kc -n "${NAMESPACE}" rollout status "deployment/${RELEASE}-${component}" --timeout 300s >/dev/null ||
    fail ready "${RELEASE}-${component} is not Ready"
done
evidence ready ok "api and controller Ready"

latest="$(find "${REPO_ROOT}/src/greenkube/core/migrations/scripts/postgres" -name '[0-9]*.sql' -exec basename {} \; |
  sort | tail -n 1 | cut -d_ -f1 | sed 's/^0*//')"
new_version="$(schema_version)"
[[ "${new_version}" == "${latest}" ]] || fail schema "schema version ${new_version}, expected ${latest}"
evidence schema ok "schema version ${old_version} -> ${new_version}"

psql_exec -f - <"${UPGRADE_DIR}/counts.sql" >"${EVIDENCE}/counts-after.txt"
diff -u "${EVIDENCE}/counts-before.txt" "${EVIDENCE}/counts-after.txt" >"${EVIDENCE}/counts.diff" ||
  fail rows_kept "row counts changed: $(tr '\n' ' ' <"${EVIDENCE}/counts.diff")"
evidence rows_kept ok "$(tr '\n' ' ' <"${EVIDENCE}/counts-after.txt")"

api_key_after="$(kc -n "${NAMESPACE}" get secret "${RELEASE}" -o jsonpath='{.data.GREENKUBE_API_KEY}')"
[[ "${api_key_after}" == "${api_key_before}" ]] || fail api_key "GREENKUBE_API_KEY changed during the upgrade"
api_key="$(printf '%s' "${api_key_before}" | base64 -d)"
local_port=38090
kc -n "${NAMESPACE}" port-forward "svc/${RELEASE}-api" "${local_port}:8000" >"${EVIDENCE}/port-forward.log" 2>&1 &
port_forward_pid=$!
code=000
for _ in $(seq 1 30); do
  code="$(printf 'Authorization: Bearer %s\n' "${api_key}" |
    curl -s -o "${EVIDENCE}/changes.json" -w '%{http_code}' -H @- "http://127.0.0.1:${local_port}/api/v2/changes?limit=1" || true)"
  [[ "${code}" == "000" ]] || break
  sleep 2
done
[[ "${code}" == "200" ]] || fail api_key "the ${FROM_VERSION} API key is refused after the upgrade (HTTP ${code})"
anonymous="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${local_port}/api/v2/changes?limit=1" || true)"
[[ "${anonymous}" == "401" || "${anonymous}" == "403" ]] ||
  fail api_key "an anonymous request answered HTTP ${anonymous}: the key check proves nothing"
evidence api_key ok "unchanged and accepted (anonymous: HTTP ${anonymous})"
kill "${port_forward_pid}" >/dev/null 2>&1 || true
port_forward_pid=""

# --- 5. Roll back: restore the pre-upgrade dump, then helm rollback ----------------------
if [[ "${UPGRADE_ROLLBACK}" == "1" ]]; then
  log "Rolling back to ${FROM_VERSION} by restoring the pre-upgrade dump..."
  for deployment in $(kc -n "${NAMESPACE}" get deployments -o name); do
    kc -n "${NAMESPACE}" scale "${deployment}" --replicas=0 >/dev/null
  done
  kc -n "${NAMESPACE}" wait --for=delete pod -l app.kubernetes.io/component=controller --timeout 180s >/dev/null 2>&1 || true
  kc -n "${NAMESPACE}" wait --for=delete pod -l app.kubernetes.io/component=api --timeout 180s >/dev/null 2>&1 || true
  # The documented restore (docs/operations/backup-restore.md#restore), verbatim.
  kc -n "${NAMESPACE}" exec -i "${RELEASE}-postgres-0" -c postgres -- sh -c '
    { printf "DROP SCHEMA IF EXISTS \"%s\" CASCADE;\n" "$1"; pg_restore --no-owner --file=-; } |
      PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        --single-transaction -v ON_ERROR_STOP=1 -q -o /dev/null' sh public \
    <"${work}/before-upgrade.dump" >"${EVIDENCE}/restore.txt" 2>&1 ||
    fail restore "restore of the pre-upgrade dump failed: $(grep -m 3 ERROR "${EVIDENCE}/restore.txt" | tr '\n' ' ')"
  restored_version="$(schema_version)"
  [[ "${restored_version}" == "${old_version}" ]] ||
    fail restore "schema version ${restored_version} after the restore, expected ${old_version}"
  psql_exec -f - <"${UPGRADE_DIR}/counts.sql" >"${EVIDENCE}/counts-restored.txt"
  diff -u "${EVIDENCE}/counts-before.txt" "${EVIDENCE}/counts-restored.txt" >"${EVIDENCE}/counts-restored.diff" ||
    fail restore "row counts differ after the restore: $(tr '\n' ' ' <"${EVIDENCE}/counts-restored.diff")"
  evidence restore ok "schema version ${new_version} -> ${restored_version}, rows kept"

  hc rollback "${RELEASE}" 1 --namespace "${NAMESPACE}" --wait --timeout "${UPGRADE_TIMEOUT}" \
    >"${EVIDENCE}/rollback.txt" 2>&1 || fail rollback "helm rollback failed: $(tail -n 5 "${EVIDENCE}/rollback.txt" | tr '\n' ' ')"
  for deployment in $(kc -n "${NAMESPACE}" get deployments -o name); do
    replicas="$(kc -n "${NAMESPACE}" get "${deployment}" -o jsonpath='{.spec.replicas}')"
    [[ "${replicas}" == "0" ]] && continue
    kc -n "${NAMESPACE}" rollout status "${deployment}" --timeout 300s >/dev/null ||
      fail rollback "${deployment} is not Ready after the rollback"
  done
  [[ "$(schema_version)" == "${old_version}" ]] || fail rollback "the ${FROM_VERSION} release changed the schema version"
  psql_exec -f - <"${UPGRADE_DIR}/counts.sql" >"${EVIDENCE}/counts-rolled-back.txt"
  diff -u "${EVIDENCE}/counts-before.txt" "${EVIDENCE}/counts-rolled-back.txt" >/dev/null ||
    fail rollback "row counts changed after the rollback"
  evidence rollback ok "${FROM_VERSION} Ready on schema version ${old_version}"
fi

log "Upgrade ${FROM_VERSION} -> working tree passed. Evidence: ${EVIDENCE}"

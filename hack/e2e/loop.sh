#!/usr/bin/env bash
# Drive one sample workload through the whole GreenKube loop, using only the
# public APIs (GreenKube REST API, Gitea API, Argo CD / Flux status):
#
#   collection -> recommendation -> plan preview -> approval -> PR on Gitea
#   -> merge (Gitea API) -> controller sync -> GreenKube sync confirmation
#   -> apply detection -> measurement (-> verdict when E2E_WAIT_VERDICT=1)
#
# Usage: loop.sh [argocd|flux]   (default: argocd)
#
# Every step is appended to ${E2E_STATE_DIR}/evidence/steps.jsonl and the API
# responses are saved next to it. The script stops at the first failed step.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd kubectl curl jq

controller="${1:-argocd}"
case "${controller}" in
  argocd) workload="${ARGO_WORKLOAD}" ;;
  flux) workload="${FLUX_WORKLOAD}" ;;
  *) die "Usage: $0 [argocd|flux]" ;;
esac
: "${E2E_WAIT_VERDICT:=1}"
: "${E2E_TIMEOUT_VERDICT:=1500}"
prefix="${controller}"

fail_step() {
  evidence "${prefix}.$1" failed "$2"
  exit 1
}

gitea_port_forward
greenkube_port_forward
GK_API_KEY="$(greenkube_api_key)"
[[ -n "${GK_API_KEY}" ]] || die "Could not read GREENKUBE_API_KEY from Secret ${GK_NAMESPACE}/${GK_RELEASE}."
export GK_API_KEY

# --- 0. GreenKube is healthy and automation is ready ---------------------------------
automation_ready() {
  local status
  status="$(gk_api GET /automation/status)" || return 1
  save_json "${prefix}-automation-status" "${status}"
  [[ "$(jq -r '.ready and .token_configured' <<<"${status}")" == "true" ]]
}
wait_until "automation ready" 300 5 automation_ready ||
  fail_step automation_ready "$(jq -c '{ready, token_configured, mode, worker: .worker.healthy, providers: [.providers[]? | {id, status, token_configured, last_error_code}]}' "${E2E_EVIDENCE_DIR}/${prefix}-automation-status.json" 2>/dev/null)"
evidence "${prefix}.automation_ready" ok "$(jq -c '{mode, providers: [.providers[] | {id, status, token_configured}], renderer: .renderer.configured}' "${E2E_EVIDENCE_DIR}/${prefix}-automation-status.json")"

# --- 1. Discovery binds the workload to the sample repository ------------------------
find_binding() {
  local cursor="" page item
  while :; do
    page="$(gk_api GET "/gitops/bindings?limit=100${cursor:+&cursor=${cursor}}")" || return 1
    item="$(jq -c --arg ns "${APPS_NAMESPACE}" --arg name "${workload}" \
      '[.items[] | select(.namespace == $ns and .workload_name == $name)][0] // empty' <<<"${page}")"
    if [[ -n "${item}" ]]; then
      printf '%s' "${item}"
      return 0
    fi
    [[ "$(jq -r .has_more <<<"${page}")" == "true" ]] || return 1
    cursor="$(jq -r '.next_cursor | @uri' <<<"${page}")"
  done
}
binding_resolved() {
  local binding
  binding="$(find_binding)" || return 1
  save_json "${prefix}-binding" "${binding}"
  [[ "$(jq -r .status <<<"${binding}")" == "resolved" ]]
}
wait_until "binding of ${APPS_NAMESPACE}/${workload}" 300 10 binding_resolved ||
  fail_step binding "$(cat "${E2E_EVIDENCE_DIR}/${prefix}-binding.json" 2>/dev/null || echo 'no binding found')"
evidence "${prefix}.binding" ok "$(jq -c '{status, binding: (.binding // {} | {controller, repo_url, path, renderer, provider_id, revision})}' "${E2E_EVIDENCE_DIR}/${prefix}-binding.json")"

# --- 2. Collection + recommendation scans until the workload is eligible -------------
scan_once() {
  local run run_id state
  run="$(gk_api POST /optimization/runs "$(jq -cn --arg ns "${APPS_NAMESPACE}" '{namespace: $ns}')")" || return 1
  run_id="$(jq -r .run_id <<<"${run}")"
  for _ in $(seq 1 60); do
    state="$(gk_api GET "/optimization/runs/${run_id}")" || return 1
    case "$(jq -r .status <<<"${state}")" in
      succeeded | failed) break ;;
    esac
    sleep 5
  done
  save_json "${prefix}-optimization-run" "${state}"
}

eligible_recommendations() {
  local recs ids id eligibility eligible=()
  recs="$(gk_api GET "/recommendations/active?namespace=${APPS_NAMESPACE}")" || return 1
  save_json "${prefix}-recommendations" "${recs}"
  ids="$(jq -r --arg name "${workload}" \
    '.[] | select(.owner_name == $name and .container_name != null
      and ((.type | ascii_downcase) == "rightsizing_cpu" or (.type | ascii_downcase) == "rightsizing_memory")) | .id' <<<"${recs}")"
  for id in ${ids}; do
    eligibility="$(gk_api GET "/recommendations/${id}/apply-pr/eligibility")" || continue
    save_json "${prefix}-eligibility-${id}" "${eligibility}"
    if [[ "$(jq -r .eligible <<<"${eligibility}")" == "true" ]]; then
      eligible+=("${id}")
    fi
  done
  ((${#eligible[@]} > 0)) || return 1
  printf '%s\n' "${eligible[@]}" >"${E2E_STATE_DIR}/${prefix}-eligible-ids"
}

scan_until_eligible() {
  scan_once || true
  eligible_recommendations
}
started=${SECONDS}
wait_until "an eligible rightsizing recommendation for ${workload}" "${E2E_TIMEOUT_RECOMMENDATION}" 20 scan_until_eligible ||
  fail_step recommendation "$(jq -c --arg name "${workload}" '[.[] | select(.owner_name == $name) | {id, type, container_name, status, confidence, risk_level, samples: .evidence.sample_count}]' "${E2E_EVIDENCE_DIR}/${prefix}-recommendations.json" 2>/dev/null)"
rec_ids=()
while IFS= read -r id; do
  [[ -n "${id}" ]] && rec_ids+=("${id}")
done <"${E2E_STATE_DIR}/${prefix}-eligible-ids"
evidence "${prefix}.collection" ok "$(jq -c '{run_id, status, trigger, recommendation_count, new_count, changed_count, sources}' "${E2E_EVIDENCE_DIR}/${prefix}-optimization-run.json" 2>/dev/null)"
evidence "${prefix}.recommendation" ok "after $((SECONDS - started))s: $(jq -c --argjson ids "$(printf '%s\n' "${rec_ids[@]}" | jq -sc 'map(tonumber)')" \
  '[.[] | select(.id as $id | $ids | index($id)) | {id, type, container_name, current_cpu_request_millicores, recommended_cpu_request_millicores, current_memory_request_bytes, recommended_memory_request_bytes, confidence, risk_level, samples: .evidence.sample_count}]' \
  "${E2E_EVIDENCE_DIR}/${prefix}-recommendations.json")"

# --- 3. Plan preview -------------------------------------------------------------------
ids_json="$(printf '%s\n' "${rec_ids[@]}" | jq -sc 'map(tonumber)')"
plan="$(gk_api POST /automation/plans "$(jq -cn --argjson ids "${ids_json}" '{recommendation_ids: $ids}')")" ||
  fail_step plan_preview "$(cat "${E2E_STATE_DIR}/last-error.json")"
save_json "${prefix}-plan-preview" "${plan}"
plan_id="$(jq -r .plan_id <<<"${plan}")"
digest="$(jq -r .digest <<<"${plan}")"
[[ "$(jq -r .status <<<"${plan}")" == "validated" && "${digest}" == sha256:* ]] ||
  fail_step plan_preview "$(jq -c '{plan_id, status, digest, policy_result}' <<<"${plan}")"
evidence "${prefix}.plan_preview" ok "$(jq -c '{plan_id, status, digest, source: {controller: .source.controller, repo: .source.repo, base_revision: .source.base_revision}, files: [.files[].path], warnings}' <<<"${plan}")"

# --- 4. Digest-bound approval ------------------------------------------------------------
approval="$(gk_api POST "/automation/plans/${plan_id}/approve" "$(jq -cn --arg d "${digest}" '{digest: $d}')")" ||
  fail_step approval "$(cat "${E2E_STATE_DIR}/last-error.json")"
save_json "${prefix}-plan-approved" "${approval}"
operation_id="$(jq -r .operation_id <<<"${approval}")"
evidence "${prefix}.approval" ok "$(jq -c '{plan_id, status, operation_id, operation_status}' <<<"${approval}")"

# --- 5. The worker opens the pull request on Gitea ---------------------------------------
operation_done() {
  local op
  op="$(gk_api GET "/automation/operations/${operation_id}")" || return 1
  save_json "${prefix}-operation" "${op}"
  case "$(jq -r .status <<<"${op}")" in
    succeeded) return 0 ;;
    failed | cancelled) return 0 ;;
  esac
  return 1
}
wait_until "operation ${operation_id}" "${E2E_TIMEOUT_PR}" 5 operation_done ||
  fail_step pull_request "$(jq -c '{status, attempts, error}' "${E2E_EVIDENCE_DIR}/${prefix}-operation.json")"
op_status="$(jq -r .status "${E2E_EVIDENCE_DIR}/${prefix}-operation.json")"
[[ "${op_status}" == "succeeded" ]] ||
  fail_step pull_request "$(jq -c '{status, attempts, error, result}' "${E2E_EVIDENCE_DIR}/${prefix}-operation.json")"

pull_requests="$(gk_api GET /automation/pull-requests)"
save_json "${prefix}-pull-requests" "${pull_requests}"
pr_record="$(jq -c --arg plan "${plan_id}" '[.[] | select(.plan_id == $plan)][0] // empty' <<<"${pull_requests}")"
[[ -n "${pr_record}" ]] || fail_step pull_request "no PR record for plan ${plan_id}"
pr_number="$(jq -r .pr_number <<<"${pr_record}")"
gitea_pr="$(gitea_api GET "/repos/${GITEA_OWNER}/${GITEA_REPO}/pulls/${pr_number}")"
save_json "${prefix}-gitea-pr" "${gitea_pr}"
gitea_files="$(gitea_api GET "/repos/${GITEA_OWNER}/${GITEA_REPO}/pulls/${pr_number}/files")"
save_json "${prefix}-gitea-pr-files" "${gitea_files}"
evidence "${prefix}.pull_request" ok "$(jq -c --argjson files "$(jq -c '[.[].filename]' <<<"${gitea_files}")" \
  '{number, title, state, html_url, head: .head.ref, base: .base.ref, files: $files}' <<<"${gitea_pr}") greenkube_pr_url=$(jq -r .pr_url <<<"${pr_record}")"

# --- 6. A reviewer merges the PR (Gitea API) ----------------------------------------------
gitea_api POST "/repos/${GITEA_OWNER}/${GITEA_REPO}/pulls/${pr_number}/merge" \
  "$(jq -cn --arg msg "Merge GreenKube rightsizing PR #${pr_number} (e2e)" '{Do: "merge", merge_message_field: $msg}')" >/dev/null ||
  fail_step merge "Gitea merge API failed"
merged_pr="$(gitea_api GET "/repos/${GITEA_OWNER}/${GITEA_REPO}/pulls/${pr_number}")"
save_json "${prefix}-gitea-pr-merged" "${merged_pr}"
merge_sha="$(jq -r .merge_commit_sha <<<"${merged_pr}")"
[[ "$(jq -r .merged <<<"${merged_pr}")" == "true" ]] || fail_step merge "$(jq -c '{state, merged}' <<<"${merged_pr}")"
evidence "${prefix}.merge" ok "PR #${pr_number} merged as ${merge_sha}"

# --- 7. The GitOps controller syncs the merge commit --------------------------------------
if [[ "${controller}" == "argocd" ]]; then
  kubectl -n "${ARGOCD_NAMESPACE}" annotate application "${ARGOCD_APP}" argocd.argoproj.io/refresh=normal --overwrite >/dev/null
  controller_synced() {
    local app
    app="$(kubectl -n "${ARGOCD_NAMESPACE}" get application "${ARGOCD_APP}" -o json)" || return 1
    save_json "${prefix}-controller" "$(jq -c '{sync: .status.sync, health: .status.health, operation: .status.operationState.phase}' <<<"${app}")"
    [[ "$(jq -r .status.sync.revision <<<"${app}")" == "${merge_sha}" &&
      "$(jq -r .status.sync.status <<<"${app}")" == "Synced" &&
      "$(jq -r .status.health.status <<<"${app}")" == "Healthy" ]]
  }
else
  kubectl -n "${FLUX_NAMESPACE}" annotate gitrepository "${FLUX_NAME}" reconcile.fluxcd.io/requestedAt="$(date +%s)" --overwrite >/dev/null
  controller_synced() {
    local ks
    ks="$(kubectl -n "${FLUX_NAMESPACE}" get kustomization "${FLUX_NAME}" -o json)" || return 1
    save_json "${prefix}-controller" "$(jq -c '{revision: .status.lastAppliedRevision, conditions: [.status.conditions[]? | {type, status, reason}]}' <<<"${ks}")"
    [[ "$(jq -r .status.lastAppliedRevision <<<"${ks}")" == *"${merge_sha}" &&
      "$(jq -r '.status.conditions[]? | select(.type == "Ready") | .status' <<<"${ks}")" == "True" ]]
  }
fi
wait_until "${controller} sync of ${merge_sha}" "${E2E_TIMEOUT_SYNC}" 5 controller_synced ||
  fail_step controller_sync "$(cat "${E2E_EVIDENCE_DIR}/${prefix}-controller.json")"
kubectl -n "${APPS_NAMESPACE}" rollout status "deployment/${workload}" --timeout=180s >/dev/null || true
live="$(kubectl -n "${APPS_NAMESPACE}" get deployment "${workload}" -o jsonpath='{.spec.template.spec.containers[0].resources.requests}')"
evidence "${prefix}.controller_sync" ok "$(cat "${E2E_EVIDENCE_DIR}/${prefix}-controller.json") live_requests=${live}"

# --- 8. GreenKube observes the merge and confirms the sync ---------------------------------
plan_status() {
  local current
  current="$(gk_api GET "/automation/plans/${plan_id}")" || return 1
  save_json "${prefix}-plan" "${current}"
  jq -r .status <<<"${current}"
}
plan_reached() {
  local wanted="$1" status
  status="$(plan_status)" || return 1
  case " ${wanted} " in
    *" ${status} "*) return 0 ;;
  esac
  return 1
}
wait_until "plan ${plan_id} merged" "${E2E_TIMEOUT_PR}" 5 plan_reached "merged synced applied" ||
  fail_step greenkube_merge_detected "plan status $(jq -r .status "${E2E_EVIDENCE_DIR}/${prefix}-plan.json")"
evidence "${prefix}.greenkube_merge_detected" ok "plan ${plan_id} status $(jq -r .status "${E2E_EVIDENCE_DIR}/${prefix}-plan.json")"

wait_until "plan ${plan_id} synced" "${E2E_TIMEOUT_SYNC}" 5 plan_reached "synced applied" ||
  fail_step greenkube_sync_confirmed "plan status $(jq -r .status "${E2E_EVIDENCE_DIR}/${prefix}-plan.json"); prs: $(gk_api GET "/recommendations/${rec_ids[0]}/pull-requests" | jq -c --arg plan "${plan_id}" '[.[] | select(.plan_id == $plan) | {status, sync_status, sync_revision, merge_commit_sha}]')"
pr_state="$(gk_api GET "/recommendations/${rec_ids[0]}/pull-requests" | jq -c --arg plan "${plan_id}" '[.[] | select(.plan_id == $plan) | {status, sync_status, sync_revision, merge_commit_sha}][0]')"
evidence "${prefix}.greenkube_sync_confirmed" ok "plan ${plan_id} $(jq -r .status "${E2E_EVIDENCE_DIR}/${prefix}-plan.json"); pr ${pr_state}"

# --- 9. Apply detection --------------------------------------------------------------------
recs_applied() {
  local id rec status
  for id in "${rec_ids[@]}"; do
    rec="$(gk_api GET "/recommendations/${id}")" || return 1
    save_json "${prefix}-recommendation-${id}" "${rec}"
    status="$(jq -r .status <<<"${rec}")"
    case "${status}" in
      applied | verifying | verified | regressed | rollback_review) ;;
      *) return 1 ;;
    esac
  done
}
wait_until "apply detection" "${E2E_TIMEOUT_APPLY}" 10 recs_applied ||
  fail_step apply_detected "$(for id in "${rec_ids[@]}"; do jq -c '{id, status, applied_at, application_method, verification_status}' "${E2E_EVIDENCE_DIR}/${prefix}-recommendation-${id}.json"; done)"
evidence "${prefix}.apply_detected" ok "$(for id in "${rec_ids[@]}"; do jq -c '{id, status, applied_at, application_method, actual_cpu_request_millicores, actual_memory_request_bytes}' "${E2E_EVIDENCE_DIR}/${prefix}-recommendation-${id}.json"; done | tr '\n' ' ')"

# --- 10. Measurement starts ---------------------------------------------------------------
measurement_started() {
  local id rec
  for id in "${rec_ids[@]}"; do
    rec="$(gk_api GET "/recommendations/${id}")" || return 1
    save_json "${prefix}-recommendation-${id}" "${rec}"
    save_json "${prefix}-events-${id}" "$(gk_api GET "/recommendations/${id}/events")"
    [[ "$(jq -r '.verification_status // ""' <<<"${rec}")" =~ ^(pending|in_progress|passed|failed|inconclusive|realized|not_realized)$ ]] || return 1
  done
}
wait_until "measurement start" 300 10 measurement_started ||
  fail_step measurement_started "$(for id in "${rec_ids[@]}"; do jq -c '{id, status, verification_status}' "${E2E_EVIDENCE_DIR}/${prefix}-recommendation-${id}.json"; done)"
evidence "${prefix}.measurement_started" ok "$(for id in "${rec_ids[@]}"; do jq -c '{id, status, verification_status}' "${E2E_EVIDENCE_DIR}/${prefix}-recommendation-${id}.json"; jq -c '[.[] | .event_type] ' "${E2E_EVIDENCE_DIR}/${prefix}-events-${id}.json"; done | tr '\n' ' ')"

# --- 11. Verdict (optional: waits for the accelerated measurement window) ------------------
if [[ "${E2E_WAIT_VERDICT}" == "1" ]]; then
  verdict_reached() {
    local id rec
    for id in "${rec_ids[@]}"; do
      rec="$(gk_api GET "/recommendations/${id}")" || return 1
      save_json "${prefix}-recommendation-${id}" "${rec}"
      save_json "${prefix}-events-${id}" "$(gk_api GET "/recommendations/${id}/events")"
      case "$(jq -r .status <<<"${rec}")" in
        verified | regressed | rollback_review | failed) ;;
        *)
          case "$(jq -r '.verification_status // ""' <<<"${rec}")" in
            passed | failed | inconclusive | not_realized) ;;
            *) return 1 ;;
          esac
          ;;
      esac
    done
  }
  if wait_until "measurement verdict" "${E2E_TIMEOUT_VERDICT}" 30 verdict_reached; then
    evidence "${prefix}.verdict" ok "$(for id in "${rec_ids[@]}"; do jq -c '{id, status, verification_status, verified_at, cost_saved, carbon_saved_co2e_grams}' "${E2E_EVIDENCE_DIR}/${prefix}-recommendation-${id}.json"; done | tr '\n' ' ')"
  else
    fail_step verdict "$(for id in "${rec_ids[@]}"; do jq -c '{id, status, verification_status}' "${E2E_EVIDENCE_DIR}/${prefix}-recommendation-${id}.json"; done)"
  fi
  save_json "${prefix}-savings" "$(gk_api GET /recommendations/savings || echo '{}')"
fi

log "Loop for ${controller}/${workload} complete. Evidence: ${E2E_EVIDENCE_DIR}"

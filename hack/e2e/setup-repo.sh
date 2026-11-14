#!/usr/bin/env bash
# Create the sample application repository on the in-cluster Gitea and seed it
# with docs/examples/quickstart/gitops-repo through the Gitea contents API (no
# local git credentials needed).
#
# Idempotent: an existing, seeded repository is left as it is (a merged
# GreenKube PR is part of its history). GITEA_RESET_REPO=1 deletes and
# recreates it. The harness only uses, resets or deletes a repository whose
# description carries "managed by hack/e2e" (or any with E2E_ADOPT=1).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd kubectl curl jq base64
require_e2e_context

gitea_port_forward
repo_path="/repos/${GITEA_OWNER}/${GITEA_REPO}"

repo_state="$(gitea_repo_state)"
if [[ "${repo_state}" == "foreign" ]]; then
  die "Gitea repository ${GITEA_OWNER}/${GITEA_REPO} exists but was not created by the harness (its description lacks \"${GITEA_REPO_MARKER}\"). Set GITEA_REPO to another name, or E2E_ADOPT=1 if it is disposable."
fi

if [[ "${GITEA_RESET_REPO}" == "1" && "${repo_state}" == "owned" ]]; then
  log "Deleting ${GITEA_OWNER}/${GITEA_REPO} (GITEA_RESET_REPO=1)..."
  gitea_api DELETE "${repo_path}" >/dev/null
  repo_state=missing
fi

# The repository must belong to GITEA_OWNER: create it as that user when the
# token is theirs, in the organization when GITEA_OWNER is one, and through
# the admin API otherwise (the token must then be an administrator's).
create_path() {
  local login
  login="$(gitea_api GET /user | jq -r .login)"
  if [[ "${login}" == "${GITEA_OWNER}" ]]; then
    printf '/user/repos'
  elif gitea_api GET "/orgs/${GITEA_OWNER}" >/dev/null 2>&1; then
    printf '/orgs/%s/repos' "${GITEA_OWNER}"
  else
    printf '/admin/users/%s/repos' "${GITEA_OWNER}"
  fi
}

if [[ "${repo_state}" == "missing" ]]; then
  path="$(create_path)"
  log "Creating ${GITEA_OWNER}/${GITEA_REPO} on Gitea (POST ${path})..."
  gitea_api POST "${path}" "$(jq -cn --arg name "${GITEA_REPO}" --arg branch "${GITEA_BRANCH}" \
    --arg description "GreenKube quickstart sample app (${GITEA_REPO_MARKER})" \
    '{name: $name, default_branch: $branch, private: false, auto_init: true, description: $description}')" \
    >"${E2E_STATE_DIR}/gitea-create.json" ||
    die "Could not create ${GITEA_OWNER}/${GITEA_REPO} with POST ${path}: the token needs access to ${GITEA_OWNER} and the scope Gitea names here: $(head -c 300 "${E2E_STATE_DIR}/gitea-create.json" 2>/dev/null)"
  [[ "$(gitea_repo_state)" == "owned" ]] || die "Gitea created the repository under another owner than ${GITEA_OWNER}."
fi

if gitea_api GET "${repo_path}/contents/apps/${ARGO_WORKLOAD}/deployment.yaml?ref=${GITEA_BRANCH}" >/dev/null 2>&1; then
  log "Repository already seeded; leaving its history untouched."
else
  log "Seeding the repository from ${QUICKSTART_DIR}/gitops-repo..."
  files='[]'
  while IFS= read -r -d '' file; do
    relative="${file#"${QUICKSTART_DIR}/gitops-repo/"}"
    operation=create
    if gitea_api GET "${repo_path}/contents/${relative}?ref=${GITEA_BRANCH}" >/dev/null 2>&1; then
      operation=update
    fi
    entry="$(jq -cn --arg path "${relative}" --arg op "${operation}" --arg content "$(base64 <"${file}" | tr -d '\n')" \
      '{operation: $op, path: $path, content: $content}')"
    if [[ "${operation}" == "update" ]]; then
      sha="$(gitea_api GET "${repo_path}/contents/${relative}?ref=${GITEA_BRANCH}" | jq -r .sha)"
      entry="$(jq -c --arg sha "${sha}" '. + {sha: $sha}' <<<"${entry}")"
    fi
    files="$(jq -c --argjson entry "${entry}" '. + [$entry]' <<<"${files}")"
  done < <(find "${QUICKSTART_DIR}/gitops-repo" -type f -print0 | sort -z)
  gitea_api POST "${repo_path}/contents" "$(jq -cn --argjson files "${files}" --arg branch "${GITEA_BRANCH}" \
    '{branch: $branch, message: "Add GreenKube quickstart sample app", files: $files}')" >/dev/null
fi

head_sha="$(gitea_api GET "${repo_path}/branches/${GITEA_BRANCH}" | jq -r .commit.id)"
evidence repository ok "${GITEA_REPO_URL} ${GITEA_BRANCH}@${head_sha}"

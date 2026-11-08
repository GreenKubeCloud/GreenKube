#!/usr/bin/env bash
# Create the sample application repository on the in-cluster Gitea and seed it
# with docs/examples/quickstart/gitops-repo through the Gitea contents API (no
# local git credentials needed).
#
# Idempotent: an existing, seeded repository is left as it is (a merged
# GreenKube PR is part of its history). GITEA_RESET_REPO=1 deletes and
# recreates it; the repository is owned by the harness.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd kubectl curl jq base64

gitea_port_forward
repo_path="/repos/${GITEA_OWNER}/${GITEA_REPO}"

repo_exists() { gitea_api GET "${repo_path}" >/dev/null 2>&1; }

if [[ "${GITEA_RESET_REPO}" == "1" ]] && repo_exists; then
  log "Deleting ${GITEA_OWNER}/${GITEA_REPO} (GITEA_RESET_REPO=1)..."
  gitea_api DELETE "${repo_path}" >/dev/null
fi

if ! repo_exists; then
  log "Creating ${GITEA_OWNER}/${GITEA_REPO} on Gitea..."
  gitea_api POST /user/repos "$(jq -cn --arg name "${GITEA_REPO}" --arg branch "${GITEA_BRANCH}" \
    '{name: $name, default_branch: $branch, private: false, auto_init: true,
      description: "GreenKube quickstart sample app (managed by hack/e2e)"}')" >/dev/null
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

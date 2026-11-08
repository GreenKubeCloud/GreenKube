#!/usr/bin/env bash
# Build the GreenKube image from this checkout inside the Minikube container
# runtime, so the cluster can run it without a registry (imagePullPolicy:
# IfNotPresent). Idempotent: Docker layer caching makes rebuilds cheap.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

need_cmd minikube docker

if [[ "${GK_SKIP_BUILD}" == "1" ]]; then
  log "GK_SKIP_BUILD=1: reusing ${GK_IMAGE_REPOSITORY}:${GK_IMAGE_TAG}."
  exit 0
fi

log "Building ${GK_IMAGE_REPOSITORY}:${GK_IMAGE_TAG} inside Minikube from ${REPO_ROOT}..."
eval "$(minikube docker-env --shell bash)"
docker build -t "${GK_IMAGE_REPOSITORY}:${GK_IMAGE_TAG}" "${REPO_ROOT}"
image_id="$(docker image inspect --format '{{.Id}}' "${GK_IMAGE_REPOSITORY}:${GK_IMAGE_TAG}")"
evidence build ok "${GK_IMAGE_REPOSITORY}:${GK_IMAGE_TAG} ${image_id} (commit $(git -C "${REPO_ROOT}" rev-parse --short HEAD))"

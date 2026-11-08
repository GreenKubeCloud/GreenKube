# GreenKube developer entry points.
#
# End-to-end loop on a live cluster (Minikube with Argo CD, Flux, Gitea and
# Prometheus). See docs/get-started/first-pr.md and hack/e2e/lib.sh for the
# settings (all overridable from the environment). Every target refuses to run
# unless kubectl points at E2E_KUBE_CONTEXT (default: minikube).

.PHONY: quickstart e2e e2e-status e2e-clean e2e-test

## Install GreenKube with the demo values next to the sample app, then stop:
## open the first pull request yourself. Deletes nothing.
quickstart:
	hack/e2e/build-image.sh
	hack/e2e/setup-repo.sh
	hack/e2e/setup-gitops.sh
	hack/e2e/install.sh

## Build the image in Minikube, install GreenKube and drive the full loop.
## Starts by removing what a previous run created (E2E_FRESH=0 keeps it).
e2e:
	hack/e2e/run.sh

## Print the evidence of the last run and the live state of the environment.
e2e-status:
	hack/e2e/status.sh

## Remove everything the e2e harness created (objects it labelled only).
e2e-clean:
	hack/e2e/cleanup.sh

## Run the pytest wrapper (requires GREENKUBE_E2E=1 and a prepared cluster).
e2e-test:
	GREENKUBE_E2E=1 uv run pytest -q -p no:cacheprovider tests/e2e

# GreenKube developer entry points.
#
# End-to-end loop on a live cluster (Minikube with Argo CD, Flux, Gitea and
# Prometheus). See docs/get-started/first-pr.md and hack/e2e/lib.sh for the
# settings (all overridable from the environment).

.PHONY: e2e e2e-status e2e-clean e2e-test

## Build the image in Minikube, install GreenKube and drive the full loop.
e2e:
	hack/e2e/run.sh

## Print the evidence of the last run and the live state of the environment.
e2e-status:
	hack/e2e/status.sh

## Remove everything the e2e harness created.
e2e-clean:
	hack/e2e/cleanup.sh

## Run the pytest wrapper (requires GREENKUBE_E2E=1 and a prepared cluster).
e2e-test:
	GREENKUBE_E2E=1 uv run pytest -q -p no:cacheprovider tests/e2e

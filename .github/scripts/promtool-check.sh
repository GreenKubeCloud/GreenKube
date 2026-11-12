#!/usr/bin/env bash
# Validate the chart's PrometheusRule with promtool (roadmap N7).
#
# Renders templates/prometheusrule.yaml with every alert group enabled, writes
# its spec as a plain Prometheus rule file, then runs `promtool check rules`
# and the alert unit tests in helm-chart/tests/promtool/. Uses a local
# promtool when installed, otherwise the pinned Prometheus image with Docker.
#
# Usage: .github/scripts/promtool-check.sh
set -euo pipefail

CHART_DIR="${CHART_DIR:-./helm-chart}"
# prom/prometheus:v3.4.0
PROMETHEUS_IMAGE="${PROMETHEUS_IMAGE:-prom/prometheus@sha256:4a61322ac1103a0e3aea2a61ef1718422a48fa046441f299d71e660a3bc71ae9}"

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

helm template greenkube "${CHART_DIR}" \
  --set monitoring.prometheusRule.enabled=true \
  --set monitoring.serviceMonitor.enabled=true \
  --show-only templates/prometheusrule.yaml >"${work}/prometheusrule.yaml"

# Keep only .spec (the "groups:" document promtool reads). The rule file is
# indented under "spec:" by two spaces; strip them.
awk 'f { sub(/^  /, ""); print } /^spec:/ { f = 1 }' "${work}/prometheusrule.yaml" >"${work}/greenkube-rules.yaml"
tests=()
for file in "${CHART_DIR}"/tests/promtool/*.yaml; do
  cp "${file}" "${work}/"
  tests+=("$(basename "${file}")")
done

if command -v promtool >/dev/null 2>&1; then
  promtool_cmd=(promtool)
  cd "${work}"
else
  promtool_cmd=(docker run --rm -v "${work}:/work" -w /work --entrypoint promtool "${PROMETHEUS_IMAGE}")
fi

"${promtool_cmd[@]}" check rules greenkube-rules.yaml
"${promtool_cmd[@]}" test rules "${tests[@]}"

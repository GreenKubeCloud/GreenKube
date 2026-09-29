#!/usr/bin/env bash
set -euo pipefail

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

run() {
  printf '\n==> %s\n' "$*"
  "$@"
}

run uv run ruff check .
run uv run ruff format --check .
run uv run pyrefly check
run uv run pytest --cov=greenkube --cov-report=term-missing

if [[ -x frontend/node_modules/.bin/vitest ]]; then
  run npm --prefix frontend test -- --run
  run npm --prefix frontend run build
else
  printf '\n==> frontend dependencies not installed; skipping frontend gate\n'
fi

if command -v helm >/dev/null 2>&1; then
  run helm lint ./helm-chart
else
  printf '\n==> Helm not installed; skipping chart gate\n'
fi

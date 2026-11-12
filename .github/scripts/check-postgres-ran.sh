#!/usr/bin/env bash
# Fail when a pytest report (run with -rs) shows PostgreSQL tests skipped for
# lack of GREENKUBE_TEST_POSTGRES_DSN (roadmap O8): CI and `make test-pg` must
# exercise both storage backends, never silently only SQLite.
#
# Usage: .github/scripts/check-postgres-ran.sh <pytest-output-file>
set -euo pipefail
report="${1:?usage: $0 <pytest-output-file>}"
if grep -E '^SKIPPED .*GREENKUBE_TEST_POSTGRES_DSN' "${report}"; then
  echo "::error::PostgreSQL tests were skipped: GREENKUBE_TEST_POSTGRES_DSN is not reaching pytest." >&2
  exit 1
fi
echo "PostgreSQL tests ran (no skip for a missing GREENKUBE_TEST_POSTGRES_DSN)."

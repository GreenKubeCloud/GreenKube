#!/usr/bin/env bash
# Print the pytest path arguments of one CI test shard, one per line.
#
# The named shards list test directories; the "rest" shard runs everything
# else under tests/ (every other directory and the top-level test modules)
# by ignoring the directories of the named shards. A directory added to
# tests/ therefore always runs in exactly one shard. The script fails when a
# listed directory is missing or listed twice.
#
# `--list` prints the shard names as a JSON array: the CI test matrix is built
# from it, so a shard added here runs without editing the workflow.
#
# Usage: .github/scripts/test-shard.sh <storage|integration|core|api|rest>
#        .github/scripts/test-shard.sh --list
set -euo pipefail

shard_dirs() {
  case "$1" in
    # Balanced on local timings: storage ~250 s, the others 120-190 s each
    # (the CI runner with coverage is several times slower).
    storage) echo "tests/storage" ;;
    integration) echo "tests/integration tests/impact" ;;
    core) echo "tests/core" ;;
    api) echo "tests/api tests/automation tests/cli tests/auth" ;;
    *) return 1 ;;
  esac
}
NAMED_SHARDS="storage integration core api"

shard="${1:?usage: $0 <${NAMED_SHARDS// /|}|rest>}"

assigned=()
for name in ${NAMED_SHARDS}; do
  for dir in $(shard_dirs "${name}"); do
    if [[ ! -d "${dir}" ]]; then
      echo "test-shard: ${dir} (shard ${name}) does not exist" >&2
      exit 1
    fi
    for seen in "${assigned[@]+"${assigned[@]}"}"; do
      if [[ "${seen}" == "${dir}" ]]; then
        echo "test-shard: ${dir} is listed in more than one shard" >&2
        exit 1
      fi
    done
    assigned+=("${dir}")
  done
done

if [[ "${shard}" == "--list" ]]; then
  printf '['
  for name in ${NAMED_SHARDS}; do
    printf '"%s",' "${name}"
  done
  printf '"rest"]\n'
elif [[ "${shard}" == "rest" ]]; then
  echo "tests"
  for dir in "${assigned[@]}"; do
    echo "--ignore=${dir}"
  done
elif dirs="$(shard_dirs "${shard}")"; then
  # shellcheck disable=SC2086 # one path per word of the list
  printf '%s\n' ${dirs}
else
  echo "test-shard: unknown shard '${shard}' (expected: ${NAMED_SHARDS} rest)" >&2
  exit 1
fi

#!/usr/bin/env bash
# scripts/release.sh — Prepare and tag a new release.
#
# Usage:
#   ./scripts/release.sh 0.3.0
#
# What it does:
#   1. Updates the version in pyproject.toml and all synced files.
#   2. Moves the [Unreleased] changelog section to a dated version header.
#   3. Commits the version bump.
#   4. Creates a signed git tag vX.Y.Z.
#   5. Prints instructions to push (so you can review first).

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <version>  (e.g. 0.3.0)"
  exit 1
fi

VERSION="$1"
TAG="v${VERSION}"
DATE=$(date +%Y-%m-%d)
ROOT=$(git rev-parse --show-toplevel)

cd "$ROOT"

echo "🔖 Preparing release ${TAG}..."

# 1. Update version in pyproject.toml (single source of truth)
sed -i '' "s/^version = \".*\"/version = \"${VERSION}\"/" pyproject.toml

# 2. Run sync_version.py to propagate to all files
pip install -q toml ruamel.yaml 2>/dev/null
python scripts/sync_version.py

# 3. Update CHANGELOG.md — move [Unreleased] to [VERSION] — DATE
if ! grep -q "## \[Unreleased\]" CHANGELOG.md; then
  echo "❌ No [Unreleased] section found in CHANGELOG.md"
  exit 1
fi

# Insert a new empty [Unreleased] section and rename the old one
sed -i '' "s/^## \[Unreleased\]/## [Unreleased]\n\n## [${VERSION}] — ${DATE}/" CHANGELOG.md

# 4. Commit
git add -A
git commit -m "chore: release ${TAG}"

# 5. Tag
git tag -a "${TAG}" -m "Release ${TAG}"

echo ""
echo "✅ Release ${TAG} prepared locally."
echo ""
echo "Review the commit, then push:"
echo "  git push origin main --tags"
echo ""
echo "This will trigger the release workflow which:"
echo "  • Builds and pushes greenkube/greenkube:${VERSION} + :latest"
echo "  • Packages and publishes the Helm chart"
echo "  • Creates a GitHub Release with changelog notes"

#!/usr/bin/env bash
# scripts/release.sh — Prepare and tag a new release.
#
# Usage:
#   ./scripts/release.sh 0.3.0
#
# What it does:
#   1. Updates the version in pyproject.toml and all synced files.
#   2. Moves the [Unreleased] changelog section to a dated version header.
#   3. Commits the version bump with the configured signing key.
#   4. Creates a signed git tag vX.Y.Z.
#   5. Prints instructions to push (so you can review first).

set -euo pipefail

if [ $# -ne 1 ]; then
  echo "Usage: $0 <version>  (e.g. 0.3.0)"
  exit 1
fi

VERSION="$1"
if ! printf '%s\n' "$VERSION" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$'; then
  echo "Version must use MAJOR.MINOR.PATCH (for example, 0.3.0)" >&2
  exit 1
fi

TAG="v${VERSION}"
DATE=$(date +%Y-%m-%d)
ROOT=$(git rev-parse --show-toplevel)

cd "$ROOT"

if [ -n "$(git status --porcelain)" ]; then
  echo "Working tree must be clean before preparing a release" >&2
  exit 1
fi

if ! git config --get user.signingkey >/dev/null && [ "$(git config --get commit.gpgsign || true)" != "true" ]; then
  echo "A signing key is required: configure user.signingkey or commit.gpgsign=true" >&2
  exit 1
fi

echo "🔖 Preparing release ${TAG}..."

# 1. Update version in pyproject.toml (single source of truth)
python - "$VERSION" <<'PY'
import pathlib
import re
import sys

path = pathlib.Path("pyproject.toml")
text = path.read_text()
updated, count = re.subn(r'^version = ".*"$', f'version = "{sys.argv[1]}"', text, count=1, flags=re.MULTILINE)
if count != 1:
    raise SystemExit("Could not find the project version in pyproject.toml")
path.write_text(updated)
PY

# 2. Run sync_version.py to propagate to all files
uv run --locked python scripts/sync_version.py

# 3. Update CHANGELOG.md — move [Unreleased] to [VERSION] — DATE
if ! grep -q "## \[Unreleased\]" CHANGELOG.md; then
  echo "❌ No [Unreleased] section found in CHANGELOG.md"
  exit 1
fi

# Insert a new empty [Unreleased] section and rename the old one
python - "$VERSION" "$DATE" <<'PY'
import pathlib
import sys

path = pathlib.Path("CHANGELOG.md")
text = path.read_text()
needle = "## [Unreleased]"
if needle not in text:
    raise SystemExit("No [Unreleased] section found in CHANGELOG.md")
path.write_text(text.replace(needle, f"{needle}\n\n## [{sys.argv[1]}] — {sys.argv[2]}", 1))
PY

# 4. Commit
git add pyproject.toml CHANGELOG.md
while IFS= read -r changed_file; do
  [ -z "$changed_file" ] || git add -- "$changed_file"
done <<EOF
$(git diff --name-only)
EOF
git commit -S -m "chore: release ${TAG}" -m "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"

# 5. Tag
git tag -s "${TAG}" -m "Release ${TAG}"

echo ""
echo "✅ Release ${TAG} prepared locally."
echo ""
echo "Review the commit, then push:"
echo "  git push origin main --tags"
echo ""
echo "This will trigger the release workflow which:"
echo "  • Builds and pushes greenkube/greenkube:${VERSION}"
echo "  • Packages and publishes the Helm chart"
echo "  • Creates a GitHub Release with changelog notes"

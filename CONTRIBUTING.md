# Contributing to GreenKube

Thank you for your interest in GreenKube! We welcome contributions of all kinds — bug reports, feature requests, documentation improvements, and code.

GreenKube is an optimization loop for Kubernetes clusters run with GitOps. It sizes container requests from measured usage, delivers the changes as pull requests, and measures the savings they produce (see [Concepts](docs/concepts.md)). These contributions are especially valuable:

- **Recommendation sources:** most tools can push their advice through the [push API](docs/sources/push.md) with no code in GreenKube; a plugin source starts from the [Source SDK](docs/sources.md).
- **GitOps layouts and action kinds:** more repository layouts, and more kinds of changes that can become a pull request.
- **Measurement methodology:** reviews of how costs, emissions and realized savings are estimated and verified.

## Getting Started

### Prerequisites

- Python 3.13+
- Node.js 22.17+ (for the frontend; CI uses Node.js 22)
- A Kubernetes cluster with Prometheus and OpenCost (for integration testing)

### Development Setup

1. **Clone the repository:**

   ```bash
   git clone https://github.com/GreenKubeCloud/GreenKube.git
   cd GreenKube
   ```

2. **Create a virtual environment and install dependencies:**

   ```bash
   uv sync          # creates .venv with the dev and test dependency groups
   source .venv/bin/activate
   ```

3. **Install pre-commit hooks:**

   ```bash
   pre-commit install
   ```

4. **Run the test suite:**

   ```bash
   pytest
   ```

5. **(Optional) Run the frontend:**

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

## Development Workflow

### Branch Model

```
main          ← stable, production-only. Every commit here is a released version.
  └── dev     ← integration branch. All PRs target dev first.
        └── feat/your-feature   ← short-lived feature/fix branches
        └── fix/your-fix
```

| Branch | Purpose | CI triggered |
|--------|---------|-------------|
| `main` | Released code only — never commit directly | `ci.yml` (lint + test) |
| `dev` | Integration — merge PRs here | `ci.yml` + `dev-build.yml` (builds `dev-<sha>` Docker image) |
| `feat/*`, `fix/*` | Short-lived work branches | `ci.yml` on PR |

**Rule:** feature/fix branches branch off `dev` and PR back to `dev`. `dev` is only merged to `main` as part of a release.

### Day-to-Day Contribution Flow

```bash
# 1. Branch off dev
git checkout dev && git pull origin dev
git checkout -b feat/my-feature

# 2. Work, commit (Conventional Commits)
git commit -m "feat(api): add pagination to timeseries endpoint"

# 3. Open a PR targeting dev
# CI runs lint + test automatically

# 4. After merge to dev, GitHub builds:
#    greenkube/greenkube:dev-<sha>
#    greenkube/greenkube:dev-latest
```

### Release Flow (maintainers only)

When `dev` is ready to ship, use the release helper:

Set `RELEASE_VERSION` to the target semantic version before running these commands.

```bash
# Merge dev into main first
git checkout main
git merge --no-ff dev -m "chore: merge dev into main for release ${RELEASE_VERSION}"

# Then run the release script (from main)
./scripts/release.sh "$RELEASE_VERSION"
```

The script refuses a dirty working tree and needs a Git signing key (`user.signingkey` or `commit.gpgsign=true`). It will:
1. Bump the version in `pyproject.toml`, refresh `uv.lock` and propagate the version to all synced files (`scripts/sync_version.py`: `Chart.yaml`, `values.yaml`, `__init__.py`, …)
2. Move the `[Unreleased]` section in `CHANGELOG.md` to a dated `[<version>]` header and regenerate the Helm chart's Artifact Hub changes list from it (`scripts/artifacthub_changes.py`)
3. Commit everything as a signed `chore: release v<version>` commit
4. Create a signed git tag `v<version>`

`pyproject.toml` is the single source of truth for the version. The API version (shown by the UI banner), the chart `version` and `appVersion`, the chart image tag and the frontend package are copied from it. `python scripts/sync_version.py --check` (also run by `tests/scripts/test_sync_version.py`) fails when one of them diverges.

Then review the commit and push:

```bash
git push origin main --tags
```

Pushing the `vX.Y.Z` tag triggers the `release.yml` workflow which:
- Runs the full test + lint + type-check suite, the 80% coverage gate, the Helm checks, the frontend tests and build, the end-to-end loop (`e2e-nightly.yml`) and the 0.3.1 upgrade test (`upgrade-test.yml`) as gates: nothing is built or published unless they all pass
- Builds and pushes the multi-arch `greenkube/greenkube:<version>` to Docker Hub, plus `:latest` when the tag is the highest release, then signs it with cosign (keyless), attaches the SPDX SBOM as a signed attestation and publishes SLSA build provenance (see [SECURITY.md](SECURITY.md#verifying-releases))
- Regenerates the chart's `artifacthub.io/changes` from the CHANGELOG, then packages, signs (Sigstore bundle) and publishes the Helm chart to GitHub Pages
- Creates a GitHub Release with the changelog notes extracted automatically, the SBOM and the signed chart package

### Docker Tag Summary

| Tag | When published | Use case |
|-----|---------------|----------|
| `dev-<sha>` | Every push to `dev` | Staging / testing latest dev work |
| `dev-latest` | Every push to `dev` | Stable pointer to latest dev |
| `<version>` | Push of `v<version>` tag | Pinned production image |
| `latest` | Push of the highest vX.Y.Z tag | Latest stable release |

### Code Style

We use [Ruff](https://docs.astral.sh/ruff/) for both linting and formatting. The pre-commit hooks enforce this automatically.

Key rules:
- Line length: 120 characters
- Imports sorted automatically (isort-compatible)
- All code must pass `ruff check`, `ruff format --check` and `pyrefly check` (type checking; also a pre-commit hook)

### Testing

- All new code should have corresponding tests
- Tests live in the `tests/` directory, mirroring the `src/greenkube/` structure
- We use `pytest` with `pytest-asyncio` for async tests
- Run tests: `pytest` or `pytest tests/path/to/test_file.py`
- Async mode is set to `auto` — async test functions are detected automatically

### Commit Messages

We follow [Conventional Commits](https://www.conventionalcommits.org/):

```
type(scope): short description

feat(api): add pagination to metrics endpoint
fix(processor): normalize cost per-step in run_range
docs(readme): add API curl examples
chore(deps): update httpx to 0.27
```

## Pull Request Process

1. Fork the repository and create your branch from `dev`
2. Make your changes with appropriate tests
3. Ensure the full test suite passes: `pytest`
4. Ensure linting and type checking pass: `ruff check .`, `ruff format --check .` and `pyrefly check`
5. Update documentation if you changed behaviour
6. Open a pull request targeting `dev` with a clear description of what and why

## Reporting Bugs

Open a [GitHub Issue](https://github.com/GreenKubeCloud/GreenKube/issues) with:

- GreenKube version (`greenkube --version`)
- Kubernetes version
- Steps to reproduce
- Expected vs. actual behaviour
- Relevant logs

## Suggesting Features

Open a [GitHub Discussion](https://github.com/GreenKubeCloud/GreenKube/discussions) to propose new features. We're especially interested in:

- New cloud provider support
- Additional recommendation types
- Integration with other tools (Grafana, Datadog, etc.)
- Reporting accuracy and export improvements

## License

By contributing, you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).

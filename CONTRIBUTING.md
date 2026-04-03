# Contributing to GreenKube

Thank you for your interest in GreenKube! We welcome contributions of all kinds — bug reports, feature requests, documentation improvements, and code.

## Getting Started

### Prerequisites

- Python 3.10+ (we recommend 3.13+)
- Node.js 20+ (for the frontend)
- A Kubernetes cluster with Prometheus and OpenCost (for integration testing)

### Development Setup

1. **Clone the repository:**

   ```bash
   git clone https://github.com/GreenKubeCloud/GreenKube.git
   cd GreenKube
   ```

2. **Create a virtual environment and install dependencies:**

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e ".[dev,test]"
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

```bash
# Merge dev into main first
git checkout main
git merge --no-ff dev -m "chore: merge dev into main for release 0.3.0"

# Then run the release script (from main)
./scripts/release.sh 0.3.0
```

The script will:
1. Bump the version in `pyproject.toml` and all synced files (`Chart.yaml`, `values.yaml`, `__init__.py`, …)
2. Move the `[Unreleased]` section in `CHANGELOG.md` to a dated `[0.3.0]` header
3. Commit everything as `chore: release v0.3.0`
4. Create an annotated git tag `v0.3.0`

Then review the commit and push:

```bash
git push origin main --tags
```

Pushing the `vX.Y.Z` tag triggers the `release.yml` workflow which:
- Runs the full test + lint suite (gate)
- Builds and pushes `greenkube/greenkube:0.3.0` + `:latest` to Docker Hub
- Packages and publishes the Helm chart to GitHub Pages
- Creates a GitHub Release with the changelog notes extracted automatically

### Docker Tag Summary

| Tag | When published | Use case |
|-----|---------------|----------|
| `dev-<sha>` | Every push to `dev` | Staging / testing latest dev work |
| `dev-latest` | Every push to `dev` | Stable pointer to latest dev |
| `0.3.0` | Push of `v0.3.0` tag | Pinned production image |
| `latest` | Push of any vX.Y.Z tag | Latest stable release |

### Code Style

We use [Ruff](https://docs.astral.sh/ruff/) for both linting and formatting. The pre-commit hooks enforce this automatically.

Key rules:
- Line length: 120 characters
- Imports sorted automatically (isort-compatible)
- All code must pass `ruff check` and `ruff format --check`

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

1. Fork the repository and create your branch from `main`
2. Make your changes with appropriate tests
3. Ensure the full test suite passes: `pytest`
4. Ensure linting passes: `ruff check .`
5. Update documentation if you changed behaviour
6. Open a pull request with a clear description of what and why

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
- CSRD/ESRS reporting improvements

## License

By contributing, you agree that your contributions will be licensed under the [Apache 2.0 License](LICENSE).

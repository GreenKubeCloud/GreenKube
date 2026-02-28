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

### Branching

- Create feature branches from `main`: `feature/your-feature`
- Create fix branches from `main`: `fix/your-fix`

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

# CLI Reference

GreenKube ships with a CLI with two runtime modes: `start` runs the collector service and `demo` launches a self-contained sample environment. Reporting and recommendations are provided by the REST API and the dashboard, not by the CLI.

## Getting started

In a Kubernetes deployment the collector container already runs `greenkube start`; commands can be run inside the pod via `kubectl exec`:

```bash
# Find the GreenKube pod name
kubectl get pods -n greenkube

# Open a shell in the pod
kubectl exec -it <pod-name> -n greenkube -- bash
```

## Commands

### `greenkube start`

Start the collector service: connects to the database, runs schema migrations, performs an initial collection, then schedules data collection, compression, dashboard summary refresh and the recommendation lifecycle job.

```
greenkube start [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--last TEXT` | Initial backfill window, e.g. `10min`, `2h`, `7d`, `3w`, `1m` (month). Only used for the initial run; scheduled runs use the normalized window. |

This is the default runtime for the collector container in the Helm chart (`command: ["greenkube"]`, `args: ["start"]`).

### `greenkube demo`

Start GreenKube in demo mode with pre-populated sample data — no live cluster required. Creates a temporary SQLite database, starts the API server and serves the dashboard.

```
greenkube demo [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--port INTEGER` | Port for the API server (default: `8000`) |
| `--days INTEGER` | Number of recent days to generate hourly workload metrics for (default: `30`); older metric and carbon-intensity history extends to two years at daily resolution |
| `--no-browser` | Do not open the browser automatically |

**Example:**

```bash
docker run --rm -p 9000:9000 greenkube/greenkube demo --no-browser --port 9000
```

### `greenkube version`

Print the GreenKube version.

## Global flags

| Flag | Description |
|------|-------------|
| `--version` | Print the GreenKube version and exit |
| `--help` | Show help for any command |

## API server

The REST API is started by the separate `greenkube-api` entry point (the API container in the Helm chart). Reports and recommendations are available through the API and the dashboard; see [API Reference](api.md) and [Recommendation lifecycle](recommendation.md).

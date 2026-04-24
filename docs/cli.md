# CLI Reference

GreenKube ships with a CLI for running reports and collecting recommendations directly from the terminal. In a Kubernetes deployment, commands are run inside the GreenKube pod via `kubectl exec`.

## Getting started

```bash
# Find the GreenKube pod name
kubectl get pods -n greenkube

# Open a shell in the pod
kubectl exec -it <pod-name> -n greenkube -- bash
```

All commands below assume you are running inside the pod.

## Commands

### `greenkube report`

Generate a FinGreenOps report. Reads data from the database and outputs it to the console or a file.

```
greenkube report [OPTIONS]
```

**Time range options:**

| Flag | Description |
|------|-------------|
| `--last TEXT` | Time window, e.g. `24h`, `7d`, `3m` (default: `24h`) |
| `--start TEXT` | Start date/time (ISO 8601 format) |
| `--end TEXT` | End date/time (ISO 8601 format) |

**Grouping options:**

| Flag | Description |
|------|-------------|
| `--hourly` | Group results by hour |
| `--daily` | Group results by day |
| `--weekly` | Group results by week |
| `--monthly` | Group results by month |
| `--yearly` | Group results by year |

**Filter options:**

| Flag | Description |
|------|-------------|
| `--namespace TEXT` | Filter by namespace |
| `--pod TEXT` | Filter by pod name |
| `--node TEXT` | Filter by node name |

**Output options:**

| Flag | Description |
|------|-------------|
| `--format TEXT` | Output format: `table` (default), `csv`, `json` |
| `--output TEXT` | Output file path (for csv/json exports) |
| `--no-aggregate` | Disable metric aggregation — show raw rows |

**CI/CD options:**

| Flag | Description |
|------|-------------|
| `--fail-on-co2 FLOAT` | Exit with code 1 if total CO₂e (grams) exceeds this threshold |
| `--fail-on-cost FLOAT` | Exit with code 1 if total cost (USD) exceeds this threshold |

**Examples:**

```bash
# Show a daily report for the last 7 days
greenkube report --last 7d --daily

# Export the last 30 days to CSV, aggregated monthly
greenkube report --last 30d --monthly --format csv --output /tmp/report.csv

# Filter by namespace and export to JSON
greenkube report --namespace production --last 7d --format json

# CI/CD gate: fail the pipeline if CO₂e exceeds 10,000 grams in the last 24h
greenkube report --last 24h --fail-on-co2 10000
```

### `greenkube recommend`

Display optimization recommendations based on collected metrics.

```
greenkube recommend [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--namespace TEXT` | Filter recommendations by namespace |
| `--last TEXT` | Time window to analyse (default: `24h`) |

**Example:**

```bash
greenkube recommend
greenkube recommend --namespace production --last 7d
```

### `greenkube start`

Start the GreenKube collector and API server. This is the default entrypoint used by the Docker image.

```
greenkube start [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--port INTEGER` | API server port (default: `8000`) |
| `--no-browser` | Do not open a browser on start |

### `greenkube demo`

Start GreenKube in demo mode with pre-populated sample data — no live cluster required.

```
greenkube demo [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--port INTEGER` | Port to listen on (default: `9000`) |
| `--no-browser` | Do not open a browser automatically |

**Example:**

```bash
docker run --rm -p 9000:9000 greenkube/greenkube demo --no-browser --port 9000
```

### Global flags

| Flag | Description |
|------|-------------|
| `--version` | Print the GreenKube version and exit |
| `--no-color` | Disable Rich formatting (useful in CI/CD) |
| `--help` | Show help for any command |

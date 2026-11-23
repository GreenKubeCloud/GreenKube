# CLI Reference

GreenKube ships with a CLI with two runtime modes: `start` runs the controller and `demo` launches a self-contained sample environment. `recompute` and the `db` commands are maintenance commands, `plan` delivers approved plans yourself, and `auth reset-password` recovers a local account. Reporting and recommendations are provided by the REST API and the dashboard, not by the CLI.

## Getting started

In a Kubernetes deployment the controller already runs `greenkube start`; commands can be run inside it via `kubectl exec`:

```bash
kubectl -n greenkube exec -it deploy/greenkube-controller -- bash
```

## Commands

### `greenkube start`

Start the controller: connects to the database, runs schema migrations (under a PostgreSQL advisory lock; `DB_RUN_MIGRATIONS=false` skips them in the API and worker), performs an initial collection, then runs every periodic loop in its loop registry: collection, compression, dashboard summary refresh, recommendation scans and lifecycle, GitOps discovery and sync, the automation readiness reconciler (capability activation, F12), the automation controller loop and plan execution (active while a repository is deliverable). See [Process topology](architecture.md#process-topology).

```
greenkube start [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--last TEXT` | Re-collect this range at startup, e.g. `10min`, `2h`, `7d`, `3w`, `1m` (month), rewriting existing rows (a stored cost is never replaced by one of a lower-ranked source, see `--force`). Without it, the initial and scheduled runs catch up every window missing since the collection watermark (bounded by `COLLECTION_MAX_BACKFILL`). |
| `--force` | With `--last` only: replace the stored rows of the range even when the new pass looks worse than them (a step misses a stored pod, or has no OpenCost cost where the stored rows had one) and whatever the stored cost source (for example list prices corrected by configured rates). Without it such a re-collection leaves the window as stored, logs it, counts it in `greenkube_collection_recollect_skipped_total` and records its steps with reason `recollect_skipped`. |

This is the runtime of the controller Deployment in the Helm chart (`command: ["greenkube"]`, `args: ["start"]`).

The process also serves its control-loop metrics (recommendation scans, verification, collection) at `/prometheus/metrics` (and `/metrics`) on `CONTROLLER_METRICS_PORT` (default `9102`; the chart sets it from `controller.metricsPort`; `0` disables the listener). Scrapes need a bearer token only when a scrape credential is set in the controller's environment: the scrape token `GREENKUBE_METRICS_TOKEN` when set, otherwise `GREENKUBE_API_KEY` (unless `AUTH_LEGACY_API_KEY=disabled`). A port that cannot be bound is logged and the controller keeps running without the listener.

### `greenkube demo`

Start GreenKube in demo mode with pre-populated sample data — no live cluster required. Creates a temporary SQLite database, starts the API server and serves the dashboard.

```
greenkube demo [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--port`, `-p INTEGER` | Port for the API server (default: `8000`) |
| `--days`, `-d INTEGER` | Number of recent days to generate hourly workload metrics for (default: `30`); older metric and carbon-intensity history extends to two years at daily resolution |
| `--no-browser` | Do not open the browser automatically |

**Example:**

```bash
docker run --rm -p 9000:9000 greenkube/greenkube demo --no-browser --port 9000
```

### `greenkube recompute`

Re-allocate raw metric rows still within retention with the current [Impact Model](methodology/impact-model.md#recalculation-b8) version, for example after an upgrade that changed the model. Energy, CO₂e, embodied emissions, modeled costs and the per-node `__idle__` rows are recomputed from the stored requests, usage, node snapshots and grid intensity. Observed OpenCost costs and hourly aggregates are kept as they are.

```
greenkube recompute --since TEXT [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--since TEXT` | Start of the range: a duration before now (`12h`, `7d`) or an ISO-8601 timestamp. Required. |
| `--until TEXT` | End of the range (exclusive), same formats; default: now. |
| `--all` | Recompute every row in the range, not only rows written by another model version. |
| `--dry-run` | Report what would change without writing. |

Run it inside the controller pod (`kubectl exec`), where the database settings are configured.

### `greenkube db copy`

Copy a SQLite database into PostgreSQL, once, when upgrading a 0.3.x SQLite install ([Upgrading](upgrading.md#from-a-sqlite-install)).

```
greenkube db copy --from sqlite:///data/greenkube_data.db --to "$DB_CONNECTION_STRING" [OPTIONS]
```

| Flag | Description |
|------|-------------|
| `--from TEXT` | Source SQLite database (`sqlite:///absolute/path` or `sqlite://relative/path`). Never written: a working copy is migrated to the current schema. |
| `--to TEXT` | Target PostgreSQL URL. The target is migrated under the controller's advisory lock. |
| `--schema TEXT` | Target schema (default `public`, or `DB_SCHEMA`). |
| `--include-metrics` | Also copy the metric history (otherwise re-collected). Caches are never copied. |
| `--batch-size INTEGER` | Rows per batch (default 1000). |
| `--dry-run` | Count the rows per table; write nothing. |
| `--work-dir PATH` | Where the working copy is written (default: the temporary directory). |
| `--allow-nonempty` | Copy into a target whose tables already hold rows the source also has. Only to resume an interrupted copy into the same database. |

Tables are copied parents first with `ON CONFLICT DO NOTHING` and their ids kept; identity sequences are moved past the copied ids. Because ids are kept, the command refuses (exit code 1, naming the tables) a target where a table the source fills already holds rows, for example after a controller ran against it: copied events, ledger entries or plans would otherwise attach to the wrong parent. A resumed copy (`--allow-nonempty`) only adds the missing rows.

### `greenkube db ledger-accept-head` and `greenkube db ledger-rotate-key`

Savings-ledger chain maintenance, run with the controller's `LEDGER_CHAIN_KEY` (and `LEDGER_CHAIN_PREVIOUS_KEYS`) in the environment. See [Savings measurement](methodology/savings-measurement.md) for when to use them.

```
greenkube db ledger-accept-head --cluster-uid TEXT --reason TEXT [--rebuild-boundaries] [--trust-entry-keys]
greenkube db ledger-rotate-key --cluster-uid TEXT --reason TEXT [--trust-entry-keys]
```

- `ledger-accept-head` accepts the current head of the cluster's ledger chain after an investigated verification failure or a restore. `--rebuild-boundaries` moves the recorded key boundaries later to match the entries (never weaker); `--trust-entry-keys` recovers a lost status row by taking the boundaries from the entries' own keys. Both are audited.
- `ledger-rotate-key` records a rotation of `LEDGER_CHAIN_KEY` (new key in `LEDGER_CHAIN_KEY`, old key in `LEDGER_CHAIN_PREVIOUS_KEYS`). `--trust-entry-keys` is needed only without a trusted status row.

### `greenkube plan apply-patch` and `greenkube plan report-delivery`

Deliver an approved plan yourself in the `patch` and `ci` delivery modes ([Automation](automation.md)).

```
greenkube plan apply-patch PLAN_ID [--api-url URL] [--repo PATH] [--remote NAME] [--fetch/--no-fetch] [--push]
greenkube plan report-delivery PLAN_ID --pr-url URL [--head-sha SHA] [--api-url URL]
```

| Flag | Description |
|------|-------------|
| `--api-url TEXT` | GreenKube URL (default: `GREENKUBE_API_URL`). |
| `--repo PATH` | `apply-patch`: local clone of the GitOps repository (default: current directory). |
| `--remote TEXT` | `apply-patch`: remote that holds the base branch (default `origin`). |
| `--fetch/--no-fetch` | `apply-patch`: fetch the base branch first (default: fetch). |
| `--push` | `apply-patch`: push the plan branch to the remote. |
| `--pr-url TEXT` | `report-delivery`: web URL of the pull request you opened. Required. |
| `--head-sha TEXT` | `report-delivery`: head commit of the pull request (CI). |

### `greenkube auth reset-password`

Give a local account (default `admin`) a new random password, printed once and to be changed at the next sign-in; the account is re-enabled and its sessions are signed out. Single sign-on accounts have no password and are refused. See [Authentication](authentication.md).

```bash
kubectl -n greenkube exec deploy/greenkube-api -- greenkube auth reset-password admin
```

### `greenkube version`

Print the GreenKube version.

### Automation worker (`greenkube.cli.automation_worker`)

With `worker.replicas` greater than 0, the chart starts dedicated plan workers as:

```bash
python -m greenkube.cli.automation_worker
```

The worker is an optional extra plan executor: it claims queued plan operations through database leases and executes them, nothing else (the webhook outbox, PR polling, branch cleanup, plan expiry and retention run in the controller). Operation leases make several replicas safe. The chart still passes the deprecated, hidden `--queue plans` flag; `plans` is its only accepted value. `--once` processes at most one plan operation and `--poll-seconds` (default `5`) sets the delay when the queue is empty. The worker exposes its authenticated control-loop metrics on `GREENKUBE_WORKER_METRICS_PORT` (Helm: `worker.metricsPort`, default `8001`). Use the Helm chart so the mode and credentials are configured consistently.

## Global flags

| Flag | Description |
|------|-------------|
| `--version` | Print the GreenKube version and exit |
| `--help` | Show help for any command |

## API server

The REST API is started by the separate `greenkube-api` entry point (the API container in the Helm chart). Reports and recommendations are available through the API and the dashboard; see [API Reference](api.md) and [Recommendation lifecycle](recommendation.md).

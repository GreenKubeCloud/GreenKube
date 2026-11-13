# Push API (`push`)

Any tool, script or vendor can contribute advice to GreenKube by pushing signals over HTTP. This is GreenKube's neutral aggregator protocol (roadmap G6).

## What it reads

`POST /api/v1/signals` takes a batch of signals in the public schema [`signal.v1alpha1`](../../schemas/signal.v1alpha1.json). The batch envelope is [`signal-batch.v1alpha1`](../../schemas/signal-batch.v1alpha1.json):

| Field | Meaning |
|---|---|
| `source_id` | `push:<client>`: `push:` then a lowercase name (`[a-z0-9][a-z0-9_.-]{0,62}`), for example `push:ci` or `push:acme-optimizer`. |
| `complete` | Required. `true`: the batch is the client's full current advice and replaces every signal it pushed before. `false`: the batch adds or refreshes signals and keeps the others. |
| `signals` | At most 5,000 signals. |

A pushed signal is a `SourceSignal` without the fields the server computes (`schema_version`, `signal_id`, `source_id`, `provenance`, `fetched_at`): `target`, `capability`, `kind`, `proposal`, `algorithm`, and optionally `evidence`, `observed_window`, `confidence`, `source_estimate`, `source_ref`, `source_version`, `engine_family` and `raw` (trimmed to 8 KiB). A client counts as at most one consensus vote per target and finding, and `engine_family` cannot multiply its votes: a client that relays another engine's advice (for example `engine_family: vpa`) joins that engine's single vote. Pushed deletions never become recommendations on their own: another source must propose the deletion too. `target.cluster_uid` may be left out; a signal about another cluster than this GreenKube's is rejected.

## How it runs

1. The API validates the batch, stores its signals in the inbox (tables `source_inbox_batches` and `source_inbox_signals`) and answers with the stable `signal_ids`.
2. Each scan, the built-in `push` source replays the signals pushed within `max_age_hours` (default 168). They are validated by the source runner and recorded in `source_runs`/`source_signals` like the signals of any source (`GET /api/v1/sources/push:<client>/signals`).
3. Pushed signals become recommendations only through consensus. A push never creates a recommendation or a pull request by itself.

Pushing is idempotent: `signal_id` is derived from the source, target, capability, kind and proposal, so pushing the same advice again refreshes the stored row instead of adding one. Rows not pushed again for 30 days are pruned.

The `push` source is `unavailable` until something was pushed, and `degraded` when the newest push is older than `max_age_hours`; its signals are then no longer used.

## Permissions

An API key with scope `signals:write` (an admin creates it with `POST /api/v1/auth/api-keys` with `{"name": "ci", "scopes": ["signals:write"]}`). The key can push signals and connector reports (KRR, Polaris), nothing else.

**A key is bound to one client.** A scoped API key may only push batches whose `source_id` is `push:<key name>`: the key name in lowercase, every character other than `[a-z0-9_.-]` replaced by `-` (a key named `ci` writes `push:ci`, a key named `Acme Optimizer` writes `push:acme-optimizer`). Any other `source_id` answers `403` with `{"code": "push_client_mismatch"}`, whatever the key's scopes (an `admin` key included), so a leaked CI key cannot overwrite or withdraw another client's advice. Signed-in administrators and the deprecated bootstrap key `GREENKUBE_API_KEY` are not bound. Create one key per client.

## Example

```bash
curl -sS -X POST "$GREENKUBE_URL/api/v1/signals" \
  -H "Authorization: Bearer $GREENKUBE_SIGNALS_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "push:ci",
    "complete": true,
    "signals": [{
      "target": {"kind": "Deployment", "namespace": "shop", "name": "api", "container": "web"},
      "capability": "container_requests",
      "kind": "RIGHTSIZING_CPU",
      "proposal": {"kind": "container_resources", "container": "web",
                   "current": {"cpu_request_m": 1000}, "proposed": {"cpu_request_m": 300}},
      "algorithm": {"name": "acme.p95", "params": {"percentile": 95, "window": "14d"}},
      "evidence": {"summary": "p95 CPU is 210m over 14 days"},
      "confidence": 0.7,
      "source_estimate": {"monthly_savings": 18.0, "currency": "USD"}
    }]
  }'
```

```json
{"batch_id": "4f0c…", "source_id": "push:ci", "complete": true, "received_at": "…",
 "accepted": 1, "signal_ids": ["9b1e…"], "withdrawn": 0, "diagnostics": []}
```

Schema errors answer `422` with an RFC 6901 JSON pointer per problem:

```json
{"detail": "The signal batch does not match schema signal-batch.v1alpha1.",
 "errors": [{"pointer": "/signals/0/proposal/proposed/cpu_request_m",
             "message": "Input should be greater than or equal to 0", "type": "greater_than_equal"}]}
```

Other answers: `400` (not JSON), `403` (key without `signals:write`, or a `source_id` other than the key's own client: `push_client_mismatch`), `409` (an incomplete batch would make the client hold more than 50,000 signals: send a complete batch), `413` (body over 8 MiB), `503` (database unavailable).

## Configuration

```yaml
sources:
  settings:
    push: { max_age_hours: 72 }   # or { enabled: false } to ignore every push
```

## Limitations

- Batches are stored as received; the scan checks the targets against the workloads GreenKube observed and reports the others (`targets_without_usage`), but does not drop them.
- The schemas are generated from the server models (`python -m greenkube.signals.connectors.push schemas`); a test keeps the published files in sync. `v1alpha1` may change before `v1`.
- A `RecommendationSignal` CRD (in-cluster pushes without HTTP) is planned, not available.

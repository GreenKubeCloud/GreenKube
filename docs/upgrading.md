# Upgrading

## Before every upgrade: back up the database

**This step is mandatory.** The controller migrates the database schema at startup, migrations only move
forward, and `helm rollback` does not undo them: the only way back from a failed upgrade is to restore a backup
taken before it ([Backup and restore](operations/backup-restore.md#roll-back-an-upgrade)).

```bash
# With scheduled backups (postgres.backup.enabled=true): run one now and wait for it.
kubectl -n greenkube create job --from=cronjob/greenkube-postgres-backup backup-before-upgrade
kubectl -n greenkube wait --for=condition=complete job/backup-before-upgrade --timeout=1h

# Without them (0.3.x has none): dump the GreenKube schema (config.db.schema, "public"
# by default) of the bundled PostgreSQL to your workstation.
kubectl -n greenkube exec greenkube-postgres-0 -c postgres -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema=public --format=custom --no-owner' \
  > greenkube-before-upgrade.dump
pg_restore --list greenkube-before-upgrade.dump > /dev/null && echo "backup readable"
kubectl -n greenkube get secret greenkube -o yaml > greenkube-secret.yaml   # store encrypted

helm -n greenkube history greenkube   # note the revision to roll back to
```

With an external PostgreSQL, use your provider's snapshot or `pg_dump --schema=<config.db.schema>` with the
same connection string. A rollback restores this dump into an emptied schema (`DROP SCHEMA ... CASCADE` and the
dump in one transaction, see [Restore](operations/backup-restore.md#restore)): `pg_restore --clean` over the
upgraded schema fails, because the objects that newer migrations created are not in the dump. Then:

- Check the free space of the data volume: a migration that backfills a large table temporarily needs about
  as much free space as that table ([Sizing](installation.md#sizing)).
- Upgrade with `--wait` and a generous `--timeout` (migrations of a large database can take minutes; the
  controller has no statement timeout while it migrates and answers its probes meanwhile).
- Upgrading the bundled PostgreSQL major version (17 to 18) is a separate, explicit procedure:
  [PostgreSQL 17 to 18 upgrade](database-upgrade.md).

## From 0.3.x to 0.4

GreenKube 0.4 replaces the two install profiles with one topology (api, controller, renderer, PostgreSQL) and
turns automation on for everyone: there is no feature flag left, only `automation.policy.pauseDelivery`. Your
data is preserved: the bundled PostgreSQL StatefulSet, its volume and its Secret keep their names.

### What changes

| 0.3.x | 0.4 |
|---|---|
| `profile: standalone` — collector and API in one pod (`<release>`) | `<release>-api` and `<release>-controller` Deployments |
| `profile: production` — api, controller, automation worker, migration Job | the same api and controller Deployments; the worker is optional (`worker.replicas`); no migration Job |
| Migrations in a Helm hook Job (production) or at every pod start | The controller migrates at startup under an advisory lock; API replicas report not-ready (`/api/v1/health` → `migrating`) until the schema is current |
| `automation.enabled`, `automation.mode` | Always on; delivery activates when Git credentials exist; `automation.policy.pauseDelivery` pauses it |
| Mandatory `automation.providers[]` with a non-empty allowlist | Providers inferred from discovered repositories; one Secret `greenkube-git-credentials`; allowlist `["@discovered"]` |
| Renderer only with automation | Renderer always deployed (25m / 96Mi) |
| SQLite in Helm (`config.db.type`, `config.db.path`, `config.persistence`) | PostgreSQL only; SQLite stays for `greenkube demo` and local development |
| Three `minSamples` knobs | One: `config.recommendations.minSamples` |
| `config.recommendations.sourcePriority`, `vpaEnabled`, `karpenterEnabled` | `sources.priority`, `sources.settings.<id>.enabled` |
| ConfigMap `<release>` (a Helm hook) | ConfigMap `<release>-config` (a regular release resource) |

### Deprecated values (accepted for this minor release)

An upgrade that reuses a 0.3.x values file keeps working: the chart maps the old keys and lists each one under
**DEPRECATED VALUES** in the install notes. In 0.5 the values schema rejects them.

| 0.3.x value | Mapped to |
|---|---|
| `profile` | ignored (one topology) |
| `replicaCount` | `api.replicas` |
| `resources`, `apiResources` | `controller.resources`, `api.resources` |
| `collector.metricsPort`, `collector.livenessProbe`, `collector.readinessProbe` | `controller.*` |
| `config.api.livenessProbe`, `config.api.readinessProbe` | `api.*`; `config.api.enabled` is ignored (the API serves the UI) |
| `production.components.api.*`, `production.components.controller.*` | `api.*`, `controller.*` (replicas, resources, probe timings) |
| `production.components.automation.*` | `worker.*` |
| `production.leaderElection.*` | `controller.leaderElection.*` (the Lease name is kept) |
| `production.migration.*` | ignored (the controller migrates) |
| `automation.enabled: false`, `automation.mode: disabled` | `automation.policy.pauseDelivery: true` |
| `automation.mode: worker` (or the production default) with automation enabled | `worker.replicas: 1` |
| `automation.workerMetricsPort` | `worker.metricsPort` |
| `rendererSandbox.*` | `renderer.*`; `rendererSandbox.enabled` is ignored |
| `automation.policy.minSamples`, `config.recommendations.verification.minSamples` | still honoured for their gate; prefer `config.recommendations.minSamples` |
| `config.recommendations.sourcePriority` | `sources.priority` |
| `config.db.type: postgres`, `config.db.path` | ignored |
| `config.db.type: sqlite` | the upgrade fails with the migration commands, see below |

Note that `automation.enabled: false` now means *discovery, previews and measurement run, no pull request is
opened*. To keep the Git tokens out of every pod, also remove `automation.providers` and do not create
`greenkube-git-credentials`.

### From a standalone or production install with PostgreSQL

Use `--reset-then-reuse-values` (Helm 3.14 or later) or pass your values file again: the new chart defaults
then apply, plus only the values you set. Plain `--reuse-values` does not work for this upgrade: Helm then
keeps the 0.3.x chart's defaults instead of the new ones, so the keys added in 0.4 (`api`, `controller`,
`renderer`, `sources`...) are missing, and the chart stops before changing anything with
`the chart defaults are missing` and these two commands.

```bash
helm repo update
helm upgrade greenkube greenkube/greenkube -n greenkube --reset-then-reuse-values --wait --timeout 10m
kubectl -n greenkube rollout status deployment/greenkube-controller
kubectl -n greenkube rollout status deployment/greenkube-api
```

Helm replaces the standalone Deployment `<release>` (or the production `<release>-automation`) with the new
components (checked on kind by `make upgrade-test`: a seeded 0.3.1 standalone release reached Ready, schema 37
to 99, every row and the API key preserved); the StatefulSet `<release>-postgres`, its claim
`data-<release>-postgres-0` and the Secret `<release>` keep their names. The PostgreSQL pod restarts once (its
image is now pinned by digest) and the controller is recreated rather than rolled (`controller.strategy:
Recreate`), so the old and the new code never write side by side while the schema migrates. The old hook ConfigMap `<release>` is no longer used; delete it once the upgrade succeeded:

```bash
kubectl -n greenkube delete configmap greenkube --ignore-not-found
```

Then move your values to the new keys at your pace (the install notes list what to change). Explicit
`automation.providers` keep working; a provider without `repositoryAllowlist` now uses
`automation.repositoryAllowlist`.

### From a SQLite install

The chart no longer runs SQLite, and an upgrade with `config.db.type=sqlite` fails before Helm changes anything,
printing these steps. Your data stays on the claim `<release>` (it was mounted at `/data`).

1. Switch to the bundled PostgreSQL, keeping the old claim and the controller stopped:

   ```bash
   helm upgrade greenkube greenkube/greenkube -n greenkube --reset-then-reuse-values \
     --set config.db.type=postgres --set controller.replicas=0
   ```

   While `config.persistence.enabled=true` stays set, the chart keeps rendering the old claim with
   `helm.sh/resource-policy: keep`, so nothing deletes it.

2. Copy the data with the one-shot Job below. `greenkube db copy` migrates a working copy of the SQLite file to
   the current schema (the source file is never written), then copies recommendations, their events, the
   savings ledger, change measurements and the GitOps state (providers, bindings, plans, pull requests) into
   PostgreSQL. Add `--include-metrics` to copy the metric history too (large; otherwise it is re-collected).
   Ids are copied as they are, so the copy refuses a target whose tables already hold rows the source also has
   (a controller that already ran against it): copy into the fresh, empty database. To resume an interrupted copy
   into the same database, rerun with `--allow-nonempty`; rows already present are skipped.

   ```yaml
   # greenkube-db-copy.yaml — kubectl -n greenkube apply -f greenkube-db-copy.yaml
   apiVersion: batch/v1
   kind: Job
   metadata:
     name: greenkube-db-copy
   spec:
     backoffLimit: 0
     template:
       metadata:
         labels:
           # Allowed through the bundled PostgreSQL NetworkPolicy (postgres.networkPolicy).
           greenkube.cloud/postgres-client: "true"
       spec:
         restartPolicy: Never
         securityContext:
           runAsNonRoot: true
           runAsUser: 10001
           fsGroup: 10001
         containers:
           - name: copy
             image: greenkube/greenkube:0.4.0   # the version you upgrade to
             command: ["greenkube", "db", "copy",
                       "--from", "sqlite:///data/greenkube_data.db",
                       "--to", "$(DB_CONNECTION_STRING)",
                       "--work-dir", "/work"]
             env:
               - name: DB_CONNECTION_STRING
                 valueFrom:
                   secretKeyRef:
                     name: greenkube          # or greenkube-values / your secrets.existingSecret
                     key: DB_CONNECTION_STRING
             volumeMounts:
               - { name: data, mountPath: /data, readOnly: true }
               - { name: work, mountPath: /work }
         volumes:
           - name: data
             persistentVolumeClaim:
               claimName: greenkube            # the former SQLite claim, named after the release
           - name: work
             emptyDir: {}
   ```

   ```bash
   kubectl -n greenkube wait --for=condition=complete job/greenkube-db-copy --timeout=30m
   kubectl -n greenkube logs job/greenkube-db-copy
   ```

   If your release used `config.db.path`, replace `/data/greenkube_data.db` with it. The working copy needs
   free space about the size of the SQLite file in `/work`. If the 0.3.x pod was killed without a clean shutdown,
   `greenkube_data.db-wal` and `-shm` files sit next to the database and SQLite needs write access to read them:
   if the Job fails with `attempt to write a readonly database` or `unable to open database file`, drop
   `readOnly: true` from the `data` mount (the source file is still never modified by the copy itself).

3. Start the controller:

   ```bash
   helm upgrade greenkube greenkube/greenkube -n greenkube --reset-then-reuse-values --set controller.replicas=1
   ```

4. Once you have checked the data, drop the old claim and the deprecated values:

   ```bash
   kubectl -n greenkube delete job greenkube-db-copy
   kubectl -n greenkube delete pvc greenkube
   helm upgrade greenkube greenkube/greenkube -n greenkube --reset-then-reuse-values \
     --set config.persistence.enabled=false
   ```

The same command works outside Kubernetes, for example to move a local development database:

```bash
greenkube db copy --from sqlite:///path/to/greenkube_data.db --to postgresql://user:pass@host:5432/greenkube
```

### Authentication

Existing users go through the initial-admin flow on their first visit: sign in as `admin` with the password of
the Secret `<release>-initial-admin` and choose a new one. Existing API keys keep working, including the
deprecated bootstrap key `GREENKUBE_API_KEY`, which stays an admin key (`auth.legacyApiKey: admin`).

The upgrade hook adds the scrape token `GREENKUBE_METRICS_TOKEN` to the Secret `<release>` before the Pods roll,
and the ServiceMonitors switch to it in the same release, so an existing Prometheus scrape keeps working. With
`secrets.existingSecret`, the ServiceMonitors keep using its `GREENKUBE_API_KEY` until you add
`GREENKUBE_METRICS_TOKEN` to it and set `secrets.existingSecretHasMetricsToken: true`. Then move scripts to scoped
keys and set `auth.legacyApiKey: metrics` or `disabled`
([The bootstrap API key](authentication.md#the-bootstrap-api-key)).

The bundled PostgreSQL now accepts connections only from the api, controller and worker Pods
(`postgres.networkPolicy`). A Job of yours that connects to it (backup, `greenkube db copy`) needs the label
`greenkube.cloud/postgres-client: "true"` on its Pod template, or a peer in
`postgres.networkPolicy.additionalIngress`.

### Checking an upgrade without a cluster

The repository renders the 0.3.1 chart and the current chart with the same values and compares what must
survive (PostgreSQL StatefulSet, claim, Service and Secret names, the Lease, selectors, the SQLite claim):

```bash
uv run pytest tests/helm/test_helm_topology.py
```

`tests/integration/test_upgrade_from_v031.py` seeds a schema at migration 0037 (the last 0.3.1 migration) with
`hack/upgrade/seed-v0.3.1.sql` and applies every current migration (needs `GREENKUBE_TEST_POSTGRES_DSN`).

### Checking an upgrade on kind

`make upgrade-test` (`hack/upgrade/upgrade-test.sh`) runs the real upgrade on a throwaway kind cluster, with its
own kubeconfig (your current kubectl context is never used): it installs the 0.3.1 chart and image, seeds the
database, runs `helm upgrade --reset-then-reuse-values --wait` to the working tree with an image built from it,
then checks that the api and the controller are Ready, the schema is at the latest migration, every seeded row
is kept and the API key is unchanged and still accepted. Finally it rolls back as
[Backup and restore](operations/backup-restore.md#roll-back-an-upgrade) describes (restore of the pre-upgrade
dump, then `helm rollback`) and checks the 0.3.1 schema version, the rows and a Ready 0.3.1 release
(`UPGRADE_ROLLBACK=0` skips it). CI runs it on pull requests that touch migrations or
the chart, and every release tag requires it (`.github/workflows/upgrade-test.yml`). Set `UPGRADE_KEEP=1` to
keep the cluster for inspection; the evidence (steps, row counts, logs on failure) lands in
`$TMPDIR/greenkube-upgrade-test/evidence`.

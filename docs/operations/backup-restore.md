# Backup and restore

GreenKube keeps everything that matters in PostgreSQL: recommendations and their history, plans and pull
requests, applied changes and their measurements, and the append-only, hash-chained savings ledger. The metrics
can be collected again; the ledger and the audit trail cannot. Back the database up on a schedule, and always
before an upgrade.

> **Rollback after an upgrade means restoring the backup, not `helm rollback`.** The controller migrates the
> schema at startup and migrations only move forward. `helm rollback` brings the previous code back but leaves
> the new schema in place; that combination is not supported. To go back, restore the dump taken before the
> upgrade, then roll the release back (see [Roll back an upgrade](#roll-back-an-upgrade)).

## What to back up

| What | Where | How |
|---|---|---|
| The GreenKube schema | bundled StatefulSet `<release>-postgres` or your external database | `pg_dump` (below) |
| Generated credentials | Secret `<release>` (`POSTGRES_PASSWORD`, `DB_CONNECTION_STRING`, `GREENKUBE_API_KEY`, session secrets) and `<release>-initial-admin` | `kubectl get secret -o yaml`, stored encrypted (or manage them with `secrets.existingSecret`) |
| Your values | your values file or GitOps repository | version control |
| Git credentials | `greenkube-git-credentials` | your secret manager |

The dump alone is not enough with the bundled PostgreSQL: the data volume and the dump are only usable with the
password of the Secret `<release>`. Keep both.

## Scheduled backups (CronJob)

```yaml
postgres:
  backup:
    enabled: true
    schedule: '17 3 * * *'   # daily, 03:17 cluster time
    keep: 7                  # dumps kept on the volume, oldest deleted first
    persistence:
      size: 20Gi             # or existingClaim: <your claim>
```

The chart then renders a CronJob `<release>-postgres-backup` and a claim `<release>-postgres-backup` (kept by
`helm uninstall`). Each run:

1. runs `pg_dump --format=custom` of the GreenKube schema (`config.db.schema`), reading `DB_CONNECTION_STRING`,
   with the TLS mode of `config.db.sslMode` (`PGSSLMODE`), so it works for the bundled and for an external
   PostgreSQL;
2. checks the archive with `pg_restore --list`;
3. renames it `greenkube-<UTC timestamp>.dump` and deletes the dumps beyond `keep`.

It runs as UID 70 with the Pod Security `restricted` profile, without a service-account token, with the same
image as the bundled PostgreSQL (`pg_dump` 18 also dumps older servers). `concurrencyPolicy: Forbid` and
`activeDeadlineSeconds` (1 hour by default) bound a stuck run. With `monitoring.prometheusRule.enabled`,
`GreenKubeBackupMissing` fires when no run has succeeded for `backupMaxAgeHours` (36 hours by default), and
`GreenKubeDataVolumeLow` / `GreenKubeDataVolumeFillingUp` watch the backup claim too.

Size the claim for `keep` dumps. A custom-format dump is compressed: expect roughly a quarter to a third of the
database size ([Sizing](../installation.md#sizing)).

List the dumps and run one now (for example right before an upgrade):

```bash
kubectl -n greenkube create job --from=cronjob/greenkube-postgres-backup backup-before-upgrade
kubectl -n greenkube wait --for=condition=complete job/backup-before-upgrade --timeout=1h
kubectl -n greenkube logs job/backup-before-upgrade
```

### Copy the dumps off the cluster

A volume of the same cluster does not survive the loss of the cluster or of its storage. Copy the dumps to an
object store. The simplest variant leaves the chart's CronJob off and runs your own, which streams `pg_dump`
straight to the bucket (here S3 with the AWS CLI; any S3-compatible store works with `--endpoint-url`):

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: greenkube-backup-s3
  namespace: greenkube
spec:
  schedule: '17 3 * * *'
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 3600
      template:
        spec:
          restartPolicy: Never
          automountServiceAccountToken: false
          securityContext: {runAsNonRoot: true, runAsUser: 70, runAsGroup: 70, fsGroup: 70, seccompProfile: {type: RuntimeDefault}}
          volumes:
            - name: dump
              emptyDir: {sizeLimit: 20Gi}
          initContainers:
            - name: pg-dump
              image: postgres:18-alpine
              env:
                - name: DB_CONNECTION_STRING
                  valueFrom: {secretKeyRef: {name: greenkube, key: DB_CONNECTION_STRING}}
              command: ["/bin/sh", "-ec"]
              args:
                - pg_dump --dbname="$DB_CONNECTION_STRING" --schema=public --format=custom --no-owner --file=/dump/greenkube.dump &&
                  pg_restore --list /dump/greenkube.dump > /dev/null
              volumeMounts: [{name: dump, mountPath: /dump}]
          containers:
            - name: upload
              image: amazon/aws-cli:2.27.0
              env:
                - name: HOME
                  value: /dump
              envFrom:
                - secretRef: {name: greenkube-backup-s3}   # AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
              command: ["/bin/sh", "-ec"]
              args:
                - aws s3 cp /dump/greenkube.dump "s3://my-bucket/greenkube/greenkube-$(date -u +%Y%m%dT%H%M%SZ).dump"
              volumeMounts: [{name: dump, mountPath: /dump}]
```

Expire old objects with a bucket lifecycle rule. With an external managed PostgreSQL (RDS, Cloud SQL, Azure
Database), prefer the provider's automated backups and point-in-time recovery, and keep a logical dump before
each upgrade.

## Back up by hand

Bundled PostgreSQL, from your workstation (the password never leaves the pod). Like the CronJob, dump the
GreenKube schema (`config.db.schema`, `public` by default):

```bash
kubectl -n greenkube exec greenkube-postgres-0 -c postgres -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --schema=public --format=custom --no-owner' \
  > greenkube-$(date -u +%Y%m%dT%H%M%SZ).dump
pg_restore --list greenkube-*.dump | head        # the archive is readable
kubectl -n greenkube get secret greenkube -o yaml > greenkube-secret.yaml   # store encrypted
```

## Restore

A restore replaces the GreenKube schema with the content of the dump: **drop the schema, then replay the dump
into it** (the dump recreates the schema), both in one transaction. Do not use `pg_restore --clean` over the
existing schema: when the dump is older than the schema (the rollback of an upgrade), the objects that newer
migrations created (tables, foreign keys, views) are not in the dump, so `--clean` cannot drop the tables they
depend on and the restore aborts, and any table it left behind would break the next upgrade.

Stop every writer first so nothing writes between the restore and the restart:

```bash
NS=greenkube RELEASE=greenkube
kubectl -n $NS scale deployment/$RELEASE-controller deployment/$RELEASE-api --replicas=0
kubectl -n $NS scale deployment/$RELEASE-worker --replicas=0 2>/dev/null || true
kubectl -n $NS patch cronjob $RELEASE-postgres-backup -p '{"spec":{"suspend":true}}' 2>/dev/null || true
```

Restore the dump into the bundled PostgreSQL. `pg_restore --file=-` turns the archive into SQL, and `psql
--single-transaction` runs the `DROP SCHEMA` and the whole dump as one transaction: on any error nothing
changes. Set `SCHEMA` to your `config.db.schema` (`public` by default):

```bash
SCHEMA=public DUMP=greenkube-20261114T031700Z.dump
kubectl -n $NS exec -i $RELEASE-postgres-0 -c postgres -- sh -c '
  { printf "DROP SCHEMA IF EXISTS \"%s\" CASCADE;\n" "$1"; pg_restore --no-owner --file=-; } |
    PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      --single-transaction -v ON_ERROR_STOP=1 -q -o /dev/null' sh "$SCHEMA" < "$DUMP"
```

With an external database, run the same pipeline with the connection string of the backup, as a role that
owns the schema (or may create it in the database), with client tools at least as new as the `pg_dump` that
wrote the archive (PostgreSQL 18 for the chart's backups):

```bash
{ printf 'DROP SCHEMA IF EXISTS "%s" CASCADE;\n' "$SCHEMA"; pg_restore --no-owner --file=- "$DUMP"; } |
  psql "$DB_CONNECTION_STRING" --single-transaction -v ON_ERROR_STOP=1 -q -o /dev/null
```

A dump from the backup claim can be read from a temporary pod that mounts `<release>-postgres-backup`, or copied
out first with `kubectl cp` from such a pod.

The procedure is tested: `tests/integration/test_upgrade_from_v031.py` dumps a seeded 0.3.1 schema, upgrades
it, restores it with this pipeline, checks the version and the rows, and upgrades it again; the kind upgrade
test (`hack/upgrade/upgrade-test.sh`) ends with the same rollback on a real release.

The restored schema has the version of the release that took the dump. Start the release that matches it (the
same chart version, or a newer one: the controller then migrates the restored schema forward), then the
writers:

```bash
kubectl -n $NS exec $RELEASE-postgres-0 -c postgres -- sh -c \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT max(version) FROM schema_migrations"'
kubectl -n $NS scale deployment/$RELEASE-controller --replicas=1      # migrates if needed, then runs the loops
kubectl -n $NS rollout status deployment/$RELEASE-controller
kubectl -n $NS scale deployment/$RELEASE-api --replicas=1             # ready once the schema is current
kubectl -n $NS patch cronjob $RELEASE-postgres-backup -p '{"spec":{"suspend":false}}' 2>/dev/null || true
```

(Use your usual replica counts, or simply `helm upgrade` with your values, which restores them.)

### Verify the ledger after a restore

The savings ledger is hash-chained per cluster: a restore that lost, reordered or altered an entry breaks the
chain. The controller's `ledger-chain` loop verifies it at startup and every `LEDGER_CHAIN_VERIFY_INTERVAL`
(1 hour by default). After the restart, check its verdict:

```bash
kubectl -n $NS port-forward svc/$RELEASE-api 8000:8000 &
curl -s -H "Authorization: Bearer $GREENKUBE_API_KEY" http://localhost:8000/api/v2/ledger/summary | jq .chain
```

`chain.ok` must be `true`, with `verified_head_seq` equal to `head_seq` and a `verified_at` after the restart.
`ok: false` names the first broken position in `failure`; the `GreenKubeLedgerChainVerificationFailed` alert
fires as well. A dump restored as a whole keeps the chain intact; a chain failure after a restore means a partial
or edited restore: restore the dump again, completely.

Also compare a few totals with what you knew before the incident: `GET /api/v1/recommendations/savings`, the
recommendation count and the newest change in `GET /api/v2/changes`.

## Roll back an upgrade

1. Before the upgrade: run a backup (the [scheduled CronJob now](#scheduled-backups-cronjob), or
   [by hand](#back-up-by-hand)) and note the chart version (`helm history`).
2. If the new release misbehaves: stop the writers, [restore](#restore) the dump taken before the upgrade, then
   `helm rollback <release> <previous revision> --wait`. The previous release starts on the schema it expects.
3. [Verify the ledger](#verify-the-ledger-after-a-restore).

Data written between the upgrade and the restore is lost (metrics are collected again for the backfill window,
`config.collection.maxBackfill`). A `helm rollback` without the restore is only safe when the upgrade did not
apply any migration (`SELECT max(version) FROM schema_migrations` unchanged).

## Related

- [Upgrading](../upgrading.md): the back-up-first upgrade procedure.
- [PostgreSQL 17 to 18 upgrade](../database-upgrade.md): in-place major-version upgrade of the bundled volume.
- [Sizing and volume expansion](../installation.md#sizing).

# PostgreSQL 17 to 18 upgrade

`scripts/pg_upgrade_17_to_18.sh` performs a non-interactive, in-place upgrade
of the GreenKube PostgreSQL PVC with `pg_upgrade --link`.

## Preconditions

An external backup is mandatory. The script first runs `BACKUP_COMMAND`, then
`BACKUP_VERIFY_COMMAND`, and stops if either fails. It then refuses to continue
while a PostgreSQL pod (label `app.kubernetes.io/component=postgres`) is
`Running`, so PostgreSQL must already be stopped when you start it. Take the
dump while the database still runs ([Upgrading](upgrading.md#before-every-upgrade-back-up-the-database)),
stop PostgreSQL, then let `BACKUP_COMMAND` move the dump to its protected
location and `BACKUP_VERIFY_COMMAND` check it there:

```sh
kubectl -n greenkube scale statefulset greenkube-postgres --replicas=0
BACKUP_COMMAND='cp greenkube-before-upgrade.dump /secure/greenkube.dump' \
BACKUP_VERIFY_COMMAND='pg_restore --list /secure/greenkube.dump > /dev/null' \
POSTGRES_ROLE=greenkube \
./scripts/pg_upgrade_17_to_18.sh greenkube
```

Use a secret manager or protected file for backup credentials. Do not put
passwords in command-line arguments or shell tracing. The verification command
must fail if the backup is missing, empty, or otherwise invalid.

The script checks the namespace, PVC (`PVC_NAME`, default
`data-greenkube-postgres-0`), Secret and key (`SECRET_NAME`, default
`greenkube`; `SECRET_KEY`, default `POSTGRES_PASSWORD`), role name, and pod
state before it creates a Job. The `POSTGRES_PASSWORD` value is read only by
Kubernetes and is never printed. `POSTGRES_ROLE` defaults to `greenkube` and can
be overridden. The Job runs as root (`runAsUser: 0`) to install the
PostgreSQL 17 binaries, so the namespace must admit it (not Pod Security
`restricted` enforcement).

## Upgrade and rollback

The Job validates a PostgreSQL 17 cluster, creates a PostgreSQL 18 cluster,
and verifies both `PG_VERSION` files after the directory swap. The old
directory remains on the PVC as `pgdata_pg17_bak`; it is not deleted by the
script.

The rollback path is explicit and non-interactive. It validates that the
active directory is version 18 and the retained backup is version 17, swaps
them, and verifies that version 17 is active:

```sh
ACTION=rollback ./scripts/pg_upgrade_17_to_18.sh greenkube
```

Run this rollback procedure in a staging namespace before production and
validate application connectivity after starting PostgreSQL 17 again (set
`postgres.image.tag` back to a PostgreSQL 17 image).
Only remove `pgdata_pg17_bak` after the upgraded database and the external
backup have been restored and verified.

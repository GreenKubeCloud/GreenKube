# PostgreSQL 17 to 18 upgrade

`scripts/pg_upgrade_17_to_18.sh` performs a non-interactive, in-place upgrade
of the GreenKube PostgreSQL PVC with `pg_upgrade --link`.

## Preconditions

An external backup is mandatory. Supply commands that create and independently
verify a backup outside the PVC:

```sh
BACKUP_COMMAND='pg_dumpall --host "$PGHOST" --username "$PGUSER" > /secure/greenkube.sql' \
BACKUP_VERIFY_COMMAND='test -s /secure/greenkube.sql' \
POSTGRES_ROLE=greenkube \
./scripts/pg_upgrade_17_to_18.sh greenkube
```

Use a secret manager or protected file for backup credentials. Do not put
passwords in command-line arguments or shell tracing. The verification command
must fail if the backup is missing, empty, or otherwise invalid.

After the backup is verified, stop the Helm release so no PostgreSQL pod is
using the PVC. The script then checks the namespace, PVC, Secret, Secret key,
role name, and pod state before it creates a Job. The `POSTGRES_PASSWORD` value
is read only by Kubernetes and is never printed. `POSTGRES_ROLE` defaults to
`greenkube` and can be overridden.

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
validate application connectivity after reinstalling the PostgreSQL 17 chart.
Only remove `pgdata_pg17_bak` after the upgraded database and the external
backup have been restored and verified.

"""Online column type upgrades of large PostgreSQL tables (NRG-06, STO-05).

A migration that changes a column type rewrites the table under an ACCESS
EXCLUSIVE lock held until the migration batch commits. For a large metric
table that would stop collection and the API for minutes. Migrations 0145
(measures to double precision) and 0147 (ids to bigint) therefore convert
small tables in place and, for a large table, only add a shadow column per
converted column (``<column>__f8`` or ``id__i8``) kept equal to the column by a
trigger, and record the work in ``online_column_upgrades``.

:func:`complete_online_column_upgrades` finishes that work without long locks:

1. backfill the shadow columns in batches of ``batch_rows`` ids, each batch in
   its own short transaction, recording progress so a restart resumes;
2. for an id: build the unique index of the shadow column concurrently and
   validate a ``NOT NULL`` check (neither blocks writers);
3. swap in one short transaction under ``lock_timeout``: drop the triggers and
   the old columns, rename the shadow columns, restore their defaults and, for
   an id, its primary key (on the index built in step 2) and its sequence.

Writers keep using the old column names throughout: the triggers copy every
inserted or updated value into the shadow column, and after the swap the name
refers to the new column. A large table with a foreign key pointing at its id
cannot be swapped: the swap fails, is logged and is retried at each start.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

#: Suffix of the shadow column of an online upgrade, by target type.
SHADOW_SUFFIXES: dict[str, str] = {"double precision": "__f8", "bigint": "__i8"}
#: Shadow suffix of the double precision measures (migration 0145).
SHADOW_SUFFIX = SHADOW_SUFFIXES["double precision"]
#: Session-level advisory lock so only one process runs the upgrades.
_ADVISORY_KEY = 0x47524E4B4F4E4C
DEFAULT_BATCH_ROWS = 50_000
DEFAULT_SWAP_LOCK_TIMEOUT_MS = 5_000


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _shadow(column: str, target_type: str) -> str:
    return column + SHADOW_SUFFIXES[target_type]


async def _pending(connection: Any) -> dict[str, dict[str, str]]:
    if not await connection.fetchval("SELECT to_regclass('online_column_upgrades') IS NOT NULL"):
        return {}
    rows = await connection.fetch(
        "SELECT table_name, column_name, target_type FROM online_column_upgrades ORDER BY table_name, column_name"
    )
    pending: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        pending[row["table_name"]][row["column_name"]] = row["target_type"]
    return dict(pending)


async def pending_online_upgrades(connection: Any) -> dict[str, list[str]]:
    """Columns still waiting for their online upgrade, by table (empty when none)."""
    return {table: list(columns) for table, columns in (await _pending(connection)).items()}


async def complete_online_column_upgrades(
    connection: Any,
    *,
    batch_rows: int = DEFAULT_BATCH_ROWS,
    swap_lock_timeout_ms: int = DEFAULT_SWAP_LOCK_TIMEOUT_MS,
) -> list[str]:
    """Backfill and swap every pending online upgrade; return the tables completed.

    Safe to call from several processes (a session advisory lock lets one run)
    and to interrupt at any point: the next call resumes after the last
    committed batch. A swap that cannot get its lock within
    ``swap_lock_timeout_ms`` is left pending and retried on the next call.
    """
    pending = await _pending(connection)
    if not pending:
        return []
    if not await connection.fetchval("SELECT pg_try_advisory_lock($1)", _ADVISORY_KEY):
        logger.info("Another process is running the online column upgrades.")
        return []
    completed: list[str] = []
    # The pool's statement_timeout (DB_STATEMENT_TIMEOUT_MS) would cancel the concurrent index
    # build and the constraint validation of a large table at every start: lift it for this session.
    previous_timeout = await connection.fetchval("SHOW statement_timeout")
    await connection.execute("SET statement_timeout = 0")
    try:
        for table, columns in pending.items():
            await _backfill(connection, table, columns, batch_rows)
            if "id" in columns:
                await _prepare_id(connection, table)
            try:
                await _swap(connection, table, columns, swap_lock_timeout_ms)
            except asyncpg.exceptions.LockNotAvailableError:
                logger.warning(
                    "Online upgrade of %s backfilled; the column swap waited more than %d ms for its lock "
                    "and runs again on the next controller start.",
                    table,
                    swap_lock_timeout_ms,
                )
                continue
            completed.append(table)
            logger.info("Online upgrade of %s complete: %s.", table, ", ".join(f"{c} {t}" for c, t in columns.items()))
    finally:
        await connection.execute("SELECT set_config('statement_timeout', $1, false)", previous_timeout)
        await connection.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_KEY)
    return completed


async def _backfill(connection: Any, table: str, columns: dict[str, str], batch_rows: int) -> None:
    done = await connection.fetchval(
        "SELECT MIN(COALESCE(backfilled_through, 0)) FROM online_column_upgrades WHERE table_name = $1", table
    )
    last_id = await connection.fetchval(f"SELECT MAX(id) FROM {_ident(table)}") or 0
    assignments = ", ".join(
        f"{_ident(_shadow(column, target))} = {_ident(column)}" for column, target in columns.items()
    )
    start = int(done or 0)
    while start < last_id:
        end = start + batch_rows
        async with connection.transaction():
            await connection.execute(f"UPDATE {_ident(table)} SET {assignments} WHERE id > $1 AND id <= $2", start, end)
            await connection.execute(
                "UPDATE online_column_upgrades SET backfilled_through = $2 WHERE table_name = $1", table, end
            )
        start = end


def _id_index(table: str) -> str:
    return f"{table}_id_i8_key"


def _id_check(table: str) -> str:
    return f"{table}_id_i8_not_null"


async def _prepare_id(connection: Any, table: str) -> None:
    """Unique index and validated NOT NULL check of ``id__i8``, built without blocking writers."""
    index = _id_index(table)
    valid = await connection.fetchval(
        "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
        "WHERE c.relname = $1 AND c.relnamespace = current_schema()::regnamespace",
        index,
    )
    if valid is False:
        # A concurrent build interrupted earlier leaves an invalid index behind.
        await connection.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_ident(index)}")
    if not valid:
        await connection.execute(
            f"CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS {_ident(index)} ON {_ident(table)} (id__i8)"
        )
    check = _id_check(table)
    exists = await connection.fetchval(
        "SELECT convalidated FROM pg_constraint WHERE conname = $1 AND conrelid = to_regclass($2)", check, table
    )
    if exists is None:
        await connection.execute(
            f"ALTER TABLE {_ident(table)} ADD CONSTRAINT {_ident(check)} CHECK (id__i8 IS NOT NULL) NOT VALID"
        )
    if not exists:
        await connection.execute(f"ALTER TABLE {_ident(table)} VALIDATE CONSTRAINT {_ident(check)}")


async def _swap(connection: Any, table: str, columns: dict[str, str], lock_timeout_ms: int) -> None:
    quoted = _ident(table)
    async with connection.transaction():
        await connection.execute(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}")
        await connection.execute("SET LOCAL statement_timeout = 0")
        await connection.execute(f"LOCK TABLE {quoted} IN ACCESS EXCLUSIVE MODE")
        defaults = {
            row["column_name"]: row["column_default"]
            for row in await connection.fetch(
                "SELECT column_name, column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = $1 AND column_name = ANY($2::text[])",
                table,
                list(columns),
            )
        }
        for trigger in (f"{table}_online_upgrade", f"{table}_online_upgrade_id"):
            await connection.execute(f"DROP TRIGGER IF EXISTS {_ident(trigger)} ON {quoted}")
            await connection.execute(f"DROP FUNCTION IF EXISTS {_ident(trigger)}()")
        sequence = None
        primary_key = None
        if "id" in columns:
            sequence = await connection.fetchval("SELECT pg_get_serial_sequence($1, 'id')", quoted)
            primary_key = await connection.fetchval(
                "SELECT conname FROM pg_constraint WHERE conrelid = to_regclass($1) AND contype = 'p'", quoted
            )
            if primary_key:
                await connection.execute(f"ALTER TABLE {quoted} DROP CONSTRAINT {_ident(primary_key)}")
            if sequence:
                # Dropping the column must not drop its sequence.
                await connection.execute(f"ALTER SEQUENCE {sequence} OWNED BY NONE")
        for column, target in columns.items():
            await connection.execute(f"ALTER TABLE {quoted} DROP COLUMN {_ident(column)}")
            await connection.execute(
                f"ALTER TABLE {quoted} RENAME COLUMN {_ident(_shadow(column, target))} TO {_ident(column)}"
            )
            default = defaults.get(column)
            if column == "id":
                continue
            if default is not None:
                # Defaults of REAL columns read back as "0" or "'1'::real": keep the value, drop the cast.
                value = default.split("::", 1)[0]
                await connection.execute(f"ALTER TABLE {quoted} ALTER COLUMN {_ident(column)} SET DEFAULT {value}")
        if "id" in columns:
            # The validated check makes SET NOT NULL skip the table scan.
            await connection.execute(f"ALTER TABLE {quoted} ALTER COLUMN id SET NOT NULL")
            await connection.execute(f"ALTER TABLE {quoted} DROP CONSTRAINT {_ident(_id_check(table))}")
            name = primary_key or f"{table}_pkey"
            await connection.execute(
                f"ALTER TABLE {quoted} ADD CONSTRAINT {_ident(name)} PRIMARY KEY USING INDEX {_ident(_id_index(table))}"
            )
            if sequence:
                await connection.execute(f"ALTER TABLE {quoted} ALTER COLUMN id SET DEFAULT nextval('{sequence}')")
                await connection.execute(f"ALTER SEQUENCE {sequence} OWNED BY {quoted}.id")
        await connection.execute("DELETE FROM online_column_upgrades WHERE table_name = $1", table)

"""Online column type upgrades of large PostgreSQL tables (NRG-06).

A migration that changes a column type rewrites the table under an ACCESS
EXCLUSIVE lock held until the migration batch commits. For a large metric
table that would stop collection and the API for minutes. Migration 0145
therefore converts small tables in place and, for a large table, only adds a
shadow column per converted column (``<column>__f8``) kept equal to the
column by a trigger, and records the work in ``online_column_upgrades``.

:func:`complete_online_column_upgrades` finishes that work without long locks:

1. backfill the shadow columns in batches of ``batch_rows`` ids, each batch in
   its own short transaction, recording progress so a restart resumes;
2. swap in one short transaction under ``lock_timeout``: drop the trigger and
   the old columns, rename the shadow columns and restore their defaults.

Writers keep using the old column names throughout: the trigger copies every
inserted or updated value into the shadow column, and after the swap the name
refers to the new column.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

#: Suffix of the shadow column of an online upgrade (migration 0145).
SHADOW_SUFFIX = "__f8"
#: Session-level advisory lock so only one process runs the upgrades.
_ADVISORY_KEY = 0x47524E4B4F4E4C
DEFAULT_BATCH_ROWS = 50_000
DEFAULT_SWAP_LOCK_TIMEOUT_MS = 5_000


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def pending_online_upgrades(connection: Any) -> dict[str, list[str]]:
    """Columns still waiting for their online upgrade, by table (empty when none)."""
    if not await connection.fetchval("SELECT to_regclass('online_column_upgrades') IS NOT NULL"):
        return {}
    rows = await connection.fetch(
        "SELECT table_name, column_name FROM online_column_upgrades ORDER BY table_name, column_name"
    )
    pending: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        pending[row["table_name"]].append(row["column_name"])
    return dict(pending)


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
    pending = await pending_online_upgrades(connection)
    if not pending:
        return []
    if not await connection.fetchval("SELECT pg_try_advisory_lock($1)", _ADVISORY_KEY):
        logger.info("Another process is running the online column upgrades.")
        return []
    completed: list[str] = []
    try:
        for table, columns in pending.items():
            await _backfill(connection, table, columns, batch_rows)
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
            logger.info("Online upgrade of %s complete: %s are double precision.", table, ", ".join(columns))
    finally:
        await connection.execute("SELECT pg_advisory_unlock($1)", _ADVISORY_KEY)
    return completed


async def _backfill(connection: Any, table: str, columns: list[str], batch_rows: int) -> None:
    done = await connection.fetchval(
        "SELECT MIN(COALESCE(backfilled_through, 0)) FROM online_column_upgrades WHERE table_name = $1", table
    )
    last_id = await connection.fetchval(f"SELECT MAX(id) FROM {_ident(table)}") or 0
    assignments = ", ".join(f"{_ident(column + SHADOW_SUFFIX)} = {_ident(column)}" for column in columns)
    start = int(done or 0)
    while start < last_id:
        end = start + batch_rows
        async with connection.transaction():
            await connection.execute(f"UPDATE {_ident(table)} SET {assignments} WHERE id > $1 AND id <= $2", start, end)
            await connection.execute(
                "UPDATE online_column_upgrades SET backfilled_through = $2 WHERE table_name = $1", table, end
            )
        start = end


async def _swap(connection: Any, table: str, columns: list[str], lock_timeout_ms: int) -> None:
    async with connection.transaction():
        await connection.execute(f"SET LOCAL lock_timeout = {int(lock_timeout_ms)}")
        await connection.execute(f"LOCK TABLE {_ident(table)} IN ACCESS EXCLUSIVE MODE")
        defaults = {
            row["column_name"]: row["column_default"]
            for row in await connection.fetch(
                "SELECT column_name, column_default FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = $1 AND column_name = ANY($2::text[])",
                table,
                columns,
            )
        }
        trigger = _ident(f"{table}_online_upgrade")
        await connection.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {_ident(table)}")
        await connection.execute(f"DROP FUNCTION IF EXISTS {trigger}()")
        for column in columns:
            shadow = column + SHADOW_SUFFIX
            await connection.execute(f"ALTER TABLE {_ident(table)} DROP COLUMN {_ident(column)}")
            await connection.execute(f"ALTER TABLE {_ident(table)} RENAME COLUMN {_ident(shadow)} TO {_ident(column)}")
            default = defaults.get(column)
            if default is not None:
                # Defaults of REAL columns read back as "0" or "'1'::real": keep the value, drop the cast.
                value = default.split("::", 1)[0]
                await connection.execute(
                    f"ALTER TABLE {_ident(table)} ALTER COLUMN {_ident(column)} SET DEFAULT {value}"
                )
        await connection.execute("DELETE FROM online_column_upgrades WHERE table_name = $1", table)

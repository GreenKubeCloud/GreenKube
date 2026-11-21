# src/greenkube/cli/db.py
"""``greenkube db copy``: move a 0.3.x SQLite database to PostgreSQL (roadmap F17).

The Helm chart runs on PostgreSQL only since 0.4. A SQLite install keeps its data
on the former claim; this command copies it once, in the one-shot Job documented
in ``docs/upgrading.md``::

    greenkube db copy --from sqlite:///data/greenkube_data.db --to "$DB_CONNECTION_STRING"

How it stays safe:

* The source file is never modified: a working copy is migrated to the current
  schema (the same SQL migrations the controller runs), so both sides have the
  same tables and columns whatever 0.3.x version wrote the file.
* The target is migrated under the controller's advisory lock, then every table
  is copied in foreign-key order with ``ON CONFLICT DO NOTHING``: a rerun copies
  only what is missing and never overwrites rows written since.
* Tables the source fills must be empty on the target. Ids are copied as they are, so a row a running
  GreenKube already wrote under the same id would make copied child rows
  (events, ledger entries, plans) attach to the wrong parent. A rerun of an
  interrupted copy into the same database is safe and needs ``--allow-nonempty``.
* Values are converted to the PostgreSQL column types (SQLite stores timestamps
  as text and booleans as integers), and identity sequences are moved past the
  copied ids so later inserts never collide.
* Recommendations, their events, the savings ledger, change measurements and the
  GitOps state (providers, bindings, plans, pull requests) are always copied.
  Metric history is large and is re-collected; ``--include-metrics`` copies it
  too. Caches are never copied: they are rebuilt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

import typer
from typing_extensions import Annotated

logger = logging.getLogger(__name__)

app = typer.Typer(name="db", help="Database maintenance (copy a SQLite database to PostgreSQL).")

#: Rebuilt from other tables or re-collected; never copied.
CACHE_TABLES = frozenset({"metrics_summary", "metrics_timeseries_cache", "namespace_cache", "schema_migrations"})
#: Metric history: copied only with --include-metrics (the controller re-collects it).
METRIC_TABLES = frozenset(
    {
        "combined_metrics",
        "combined_metrics_hourly",
        "container_metrics",
        "container_metrics_hourly",
        "carbon_intensity_history",
        "node_idle_metrics",
        "node_idle_metrics_hourly",
        "node_power_consumption",
        "node_snapshots",
        "node_snapshots_scd",
        "pod_resource_usage",
        "workload_usage_hourly",
        "workload_usage_rollup_marks",
        "metrics_compression_watermarks",
        "metrics_dirty_hours",
        "collection_watermarks",
        "collection_windows",
        "instance_carbon_profiles",
    }
)
DEFAULT_BATCH_SIZE = 1000


class TargetNotEmptyError(RuntimeError):
    """The target already holds rows in tables the copy writes (see ``--allow-nonempty``)."""

    def __init__(self, tables: Sequence[str]) -> None:
        self.tables = list(tables)
        super().__init__(
            "The target database already holds rows in: "
            + ", ".join(self.tables)
            + ". Copy into an empty database: ids are copied as they are, so rows a running GreenKube wrote "
            "would collide with copied ids and attach copied events, ledger entries or plans to the wrong parent. "
            "If these rows come from an interrupted run of this same copy, rerun with --allow-nonempty "
            "(rows already present are skipped)."
        )


@dataclass
class TableReport:
    table: str
    source_rows: int = 0
    copied: int = 0
    skipped: int = 0


@dataclass
class CopyReport:
    tables: list[TableReport] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def copied(self) -> int:
        return sum(table.copied for table in self.tables)


def sqlite_path_from_url(url: str) -> Path:
    """``sqlite:///data/greenkube_data.db`` (absolute) or ``sqlite://relative.db``."""
    if not url.startswith("sqlite://"):
        raise typer.BadParameter("--from must be a sqlite:// URL, e.g. sqlite:///data/greenkube_data.db")
    path = url.removeprefix("sqlite://")
    if not path:
        raise typer.BadParameter("--from needs a database path.")
    return Path(path)


def select_tables(
    source: Iterable[str], target: Iterable[str], *, include_metrics: bool
) -> tuple[list[str], list[str]]:
    """Tables present on both sides, minus caches (and metrics unless requested); plus the excluded ones."""
    common = sorted(set(source) & set(target))
    selected, excluded = [], []
    for table in common:
        if table in CACHE_TABLES or (table in METRIC_TABLES and not include_metrics):
            excluded.append(table)
        else:
            selected.append(table)
    return selected, excluded


def order_by_foreign_keys(tables: Sequence[str], references: Iterable[tuple[str, str]]) -> list[str]:
    """Parents before children; ``references`` are ``(child, parent)`` pairs (self-references ignored)."""
    pending = {table: set() for table in tables}
    for child, parent in references:
        if child in pending and parent in pending and child != parent:
            pending[child].add(parent)
    ordered: list[str] = []
    while pending:
        ready = sorted(table for table, parents in pending.items() if not parents - set(ordered))
        if not ready:  # a cycle: keep a stable order, inserts may then need --batch-size 1 retries
            ready = sorted(pending)[:1]
        for table in ready:
            ordered.append(table)
            pending.pop(table)
    return ordered


def convert_value(value: Any, data_type: str) -> Any:
    """Convert one SQLite value to what asyncpg expects for a PostgreSQL column type."""
    if value is None:
        return None
    kind = data_type.lower()
    if kind.startswith("timestamp"):
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
        else:
            text = str(value).strip().replace(" ", "T", 1)
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed if "with time zone" in kind else parsed.astimezone(timezone.utc).replace(tzinfo=None)
    if kind == "date":
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    if kind == "boolean":
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "t", "yes"}
        return bool(value)
    if kind in {"integer", "bigint", "smallint"}:
        return int(value)
    if kind in {"double precision", "real"}:
        return float(value)
    if kind == "numeric":
        return Decimal(str(value))
    if kind in {"json", "jsonb"}:
        return value if isinstance(value, str) else json.dumps(value)
    if kind == "bytea":
        return bytes(value) if not isinstance(value, str) else value.encode()
    if kind == "array" or kind.endswith("[]"):
        return json.loads(value) if isinstance(value, str) else list(value)
    return value if isinstance(value, str) else str(value)


async def _prepare_source(source: Path, work_dir: Path | None) -> Path:
    """Copy the SQLite file (and its WAL) and migrate the copy to the current schema."""
    import aiosqlite

    from greenkube.core.migrations import MigrationRunner

    if not source.is_file():
        raise typer.BadParameter(f"SQLite database {source} does not exist.")
    directory = Path(tempfile.mkdtemp(prefix="greenkube-db-copy-", dir=str(work_dir) if work_dir else None))
    working = directory / source.name
    # The backup API reads a consistent snapshot, WAL included, without writing the source.
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as reader, sqlite3.connect(working) as writer:
        reader.backup(writer)
    async with aiosqlite.connect(working) as connection:
        await MigrationRunner("sqlite").run(connection)
    return working


async def copy_sqlite_to_postgres(
    source: Path,
    dsn: str,
    *,
    schema: str = "public",
    include_metrics: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    dry_run: bool = False,
    work_dir: Path | None = None,
    echo: Any = None,
    allow_nonempty: bool = False,
) -> CopyReport:
    import aiosqlite
    import asyncpg

    from greenkube.core.migrations import MigrationRunner

    say = echo or (lambda message: logger.info(message))
    if not schema.replace("_", "").isalnum():
        raise typer.BadParameter("--schema contains invalid identifier characters")
    working = await _prepare_source(source, work_dir)
    report = CopyReport(dry_run=dry_run)
    target = await asyncpg.connect(dsn, server_settings={"search_path": schema, "timezone": "UTC"})
    try:
        if not dry_run:
            await target.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
            await MigrationRunner("postgres").run(target)
        async with aiosqlite.connect(working) as reader:
            source_tables = [
                row[0]
                for row in await (
                    await reader.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                    )
                ).fetchall()
            ]
            # A dry run neither migrates nor writes the target: plan from the (migrated) source.
            target_tables = (
                source_tables
                if dry_run
                else [
                    row["table_name"]
                    for row in await target.fetch(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = $1 AND table_type = 'BASE TABLE'",
                        schema,
                    )
                ]
            )
            selected, report.excluded = select_tables(source_tables, target_tables, include_metrics=include_metrics)
            references = [
                (row["child"], row["parent"])
                for row in await target.fetch(
                    "SELECT c.conrelid::regclass::text AS child, c.confrelid::regclass::text AS parent "
                    "FROM pg_constraint c JOIN pg_namespace n ON n.oid = c.connamespace "
                    "WHERE c.contype = 'f' AND n.nspname = $1",
                    schema,
                )
            ]
            references = [(child.split(".")[-1], parent.split(".")[-1]) for child, parent in references]
            if not dry_run and not allow_nonempty:
                # Only tables with rows on both sides can collide; an admin the API created
                # while the source has no account rows is harmless.
                occupied = [
                    table
                    for table in selected
                    if await target.fetchval(f'SELECT EXISTS (SELECT 1 FROM "{table}")')
                    and await (await reader.execute(f'SELECT 1 FROM "{table}" LIMIT 1')).fetchone() is not None
                ]
                if occupied:
                    raise TargetNotEmptyError(occupied)
            for table in order_by_foreign_keys(selected, references):
                report.tables.append(
                    await _copy_table(reader, target, schema, table, batch_size=batch_size, dry_run=dry_run)
                )
                last = report.tables[-1]
                say(f"{table}: {last.source_rows} row(s), {last.copied} copied, {last.skipped} already present")
            if not dry_run:
                await _advance_sequences(target, schema, selected)
    finally:
        await target.close()
        shutil.rmtree(working.parent, ignore_errors=True)
    return report


async def _copy_table(reader, target, schema: str, table: str, *, batch_size: int, dry_run: bool) -> TableReport:
    report = TableReport(table)
    if dry_run:
        report.source_rows = (await (await reader.execute(f'SELECT COUNT(*) FROM "{table}"')).fetchone())[0]
        return report
    columns = await target.fetch(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = $1 AND table_name = $2 ORDER BY ordinal_position",
        schema,
        table,
    )
    target_types = {row["column_name"]: row["data_type"] for row in columns}
    cursor = await reader.execute(f'SELECT * FROM "{table}"')
    names = [column[0] for column in cursor.description or []]
    shared = [name for name in names if name in target_types]
    if not shared:
        return report
    column_list = ", ".join(f'"{name}"' for name in shared)
    # Each batch is bulk-loaded into a transaction-scoped staging table, then inserted
    # with ON CONFLICT DO NOTHING: reruns are idempotent and the status gives the count.
    statement = (
        f'INSERT INTO "{table}" ({column_list}) SELECT {column_list} FROM "_greenkube_copy" ON CONFLICT DO NOTHING'
    )
    positions = [names.index(name) for name in shared]
    while True:
        rows = await cursor.fetchmany(batch_size)
        if not rows:
            break
        report.source_rows += len(rows)
        if dry_run:
            continue
        records = [tuple(convert_value(row[i], target_types[names[i]]) for i in positions) for row in rows]
        async with target.transaction():
            await target.execute(f'CREATE TEMP TABLE "_greenkube_copy" (LIKE "{table}") ON COMMIT DROP')
            await target.copy_records_to_table("_greenkube_copy", records=records, columns=shared)
            status = await target.execute(statement)
        inserted = int(str(status).rsplit(" ", 1)[-1])
        report.copied += inserted
        report.skipped += len(records) - inserted
    await cursor.close()
    return report


async def _advance_sequences(target, schema: str, tables: Sequence[str]) -> None:
    rows = await target.fetch(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = $1 AND (column_default LIKE 'nextval(%' OR is_identity = 'YES')",
        schema,
    )
    for row in rows:
        table, column = row["table_name"], row["column_name"]
        if table not in tables:
            continue
        sequence = await target.fetchval("SELECT pg_get_serial_sequence($1, $2)", f"{schema}.{table}", column)
        if sequence is None:
            continue
        await target.execute(
            f'SELECT setval($1, COALESCE((SELECT MAX("{column}") FROM "{table}"), 1), '
            f'(SELECT MAX("{column}") FROM "{table}") IS NOT NULL)',
            sequence,
        )


@app.command("copy")
def copy_command(
    source: Annotated[str, typer.Option("--from", help="Source database, e.g. sqlite:///data/greenkube_data.db")],
    target: Annotated[str, typer.Option("--to", help="Target PostgreSQL URL (postgresql://user:pass@host/db)")],
    schema: Annotated[str, typer.Option("--schema", envvar="DB_SCHEMA", help="Target PostgreSQL schema.")] = "public",
    include_metrics: Annotated[
        bool, typer.Option("--include-metrics", help="Also copy the metric history (large; re-collected otherwise).")
    ] = False,
    batch_size: Annotated[int, typer.Option("--batch-size", min=1, max=50_000)] = DEFAULT_BATCH_SIZE,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Count rows only; write nothing.")] = False,
    work_dir: Annotated[
        Path | None, typer.Option("--work-dir", help="Where the migrated working copy of the source is written.")
    ] = None,
    allow_nonempty: Annotated[
        bool,
        typer.Option(
            "--allow-nonempty",
            help="Copy into a target that already holds rows (only to resume an interrupted copy).",
        ),
    ] = False,
) -> None:
    """Copy a GreenKube SQLite database into PostgreSQL (one-shot upgrade from 0.3.x)."""
    if not target.startswith(("postgresql://", "postgres://")):
        raise typer.BadParameter("--to must be a postgresql:// URL")
    try:
        report = asyncio.run(
            copy_sqlite_to_postgres(
                sqlite_path_from_url(source),
                target,
                schema=schema,
                include_metrics=include_metrics,
                batch_size=batch_size,
                dry_run=dry_run,
                work_dir=work_dir,
                echo=typer.echo,
                allow_nonempty=allow_nonempty,
            )
        )
    except TargetNotEmptyError as error:
        typer.echo(f"Refused: {error}", err=True)
        raise typer.Exit(code=1) from error
    verb = "would copy" if dry_run else "copied"
    typer.echo(f"Done: {verb} {sum(t.source_rows if dry_run else t.copied for t in report.tables)} row(s).")
    if report.excluded:
        typer.echo(f"Not copied (caches, or metrics without --include-metrics): {', '.join(report.excluded)}")

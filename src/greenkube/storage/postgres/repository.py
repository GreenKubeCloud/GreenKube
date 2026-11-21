import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

from ...core.exceptions import QueryError
from ...core.observability import record_metric_provenance
from ...models.metrics import CombinedMetric
from ...models.provenance import INTENSITY_FALLBACK_REASON_PREFIX
from ...utils.time import ensure_utc
from ..base_repository import (
    CarbonIntensityRepository,
    CombinedMetricsRepository,
    ContainerMetricsRepository,
    IntensityPoint,
    WindowStepProfile,
    is_persistable_intensity_record,
    measured_intensity_sql,
    window_step_profiles,
)
from ..container_rows import complete_container_rows, container_rows_of
from ..write_guards import (
    IDLE_COST_COLUMNS,
    cost_guard_assignments,
    forced_replace,
    produced_idle_keys,
    produced_pod_keys,
)

logger = logging.getLogger(__name__)

#: Rows per executemany call; a window is still written in one transaction (STO-03).
_EXECUTEMANY_CHUNK = 5000

#: Provenance and cost-component columns shared by every metrics table (A4, A11).
PROVENANCE_COLUMNS: tuple[str, ...] = (
    "cost_cpu",
    "cost_ram",
    "cost_storage",
    "cost_network",
    "cost_lb",
    "cost_source",
    "usage_source",
    "power_source",
    "intensity_source",
)


def _provenance_values(metric: CombinedMetric) -> tuple[Any, ...]:
    return tuple(getattr(metric, column) for column in PROVENANCE_COLUMNS)


#: Impact Model allocation columns of raw metric rows (B2, B5, migration 0050).
ALLOCATION_COLUMNS: tuple[str, ...] = ("model_version", "gpu_power_watts")


def _allocation_values(metric: CombinedMetric) -> tuple[Any, ...]:
    return tuple(getattr(metric, column) for column in ALLOCATION_COLUMNS)


#: Where the node and pod metadata of a raw row came from (WP-01.1, migration 0120).
ROW_SOURCE_COLUMNS: tuple[str, ...] = ("node_source", "metadata_source", "embodied_source", "gpu_source")


def _row_source_values(metric: CombinedMetric) -> tuple[Any, ...]:
    return tuple(getattr(metric, column) for column in ROW_SOURCE_COLUMNS)


def _metadata_upsert(table: str) -> str:
    """Owner, requests and provenance SET clauses, and the guard of raw metric upserts (COL-01, review 4).

    A re-collected row without pod metadata (``metadata_source = 'missing'``)
    never replaces a stored row that had metadata: its energy, CO2e, cost and
    flags were computed with request 0, so mixing them with the stored owner
    and requests would make the row inconsistent.  The whole stored row and
    its container rows are kept instead.
    """
    has_metadata = (
        f"{table}.owner_kind IS NOT NULL OR COALESCE({table}.cpu_request, 0) > 0 "
        f"OR COALESCE({table}.memory_request, 0) > 0"
    )
    assignments = ",\n".join(
        f"{column} = EXCLUDED.{column}"
        for column in ("owner_kind", "owner_name", "cpu_request", "memory_request", "metadata_source", "node_source")
    )
    return f"{assignments}\nWHERE NOT (COALESCE(EXCLUDED.metadata_source, '') = 'missing' AND ({has_metadata}))"


#: Columns of ``node_idle_metrics`` (``greenkube.impact.rows.NodeIdleMetric`` fields, migration 0050).
IDLE_METRIC_COLUMNS: tuple[str, ...] = (
    "node",
    "timestamp",
    "duration_seconds",
    "scope",
    "joules",
    "co2e_grams",
    "embodied_co2e_grams",
    "total_cost",
    "cost_cpu",
    "cost_ram",
    "cost_source",
    "cpu_unreserved_cores",
    "memory_unreserved_bytes",
    "gpu_power_watts",
    "pue",
    "grid_intensity",
    "emaps_zone",
    "power_source",
    "intensity_source",
    "model_version",
    "embodied_source",
    "gpu_source",
    "host_share",
    "node_cpu_used_cores",
)


def _utc_trunc(unit: str, column: str) -> str:
    """``date_trunc`` in UTC whatever the session time zone (STO-10)."""
    return f"(date_trunc('{unit}', {column} AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"


def _container_metric_from_row(
    row: Mapping[str, Any],
    *,
    timestamp_column: str,
    hourly: bool = False,
) -> CombinedMetric:
    data = dict(row)
    data.pop("id", None)
    if hourly:
        # sample_count is kept: hourly rows stand for several raw samples (BUG-29).
        data["timestamp"] = data.pop(timestamp_column, None)
        data["cpu_usage_millicores"] = data.pop("cpu_usage_avg", None)
        data["cpu_usage_max_millicores"] = data.pop("cpu_usage_max", None)
        data["memory_usage_bytes"] = data.pop("memory_usage_avg", None)
        data["memory_usage_max_bytes"] = data.pop("memory_usage_max", None)
    if isinstance(data.get("estimation_reasons"), str):
        try:
            data["estimation_reasons"] = json.loads(data["estimation_reasons"])
        except json.JSONDecodeError:
            data["estimation_reasons"] = []
    return CombinedMetric(**data)


class PostgresCarbonIntensityRepository(CarbonIntensityRepository):
    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def get_for_zone_at_time(self, zone: str, timestamp: str) -> Optional[float]:
        try:
            async with self.db_manager.connection_scope() as conn:
                # Parse timestamp string to datetime object if needed
                if isinstance(timestamp, datetime):
                    ts = timestamp
                else:
                    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))

                # Use $n placeholders for asyncpg. Values older than
                # INTENSITY_MAX_AGE are stale and never used silently (A8).
                from ...core.config import get_config

                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                query = f"""
                    SELECT carbon_intensity
                    FROM carbon_intensity_history
                    WHERE zone = $1 AND datetime <= $2 AND datetime >= $3
                      AND {measured_intensity_sql()}
                    ORDER BY datetime DESC
                    LIMIT 1
                """
                row = await conn.fetchrow(query, zone, ts, ts - get_config().intensity_max_age)
                if row:
                    return row["carbon_intensity"]
                return None
        except Exception as e:
            logger.error("Error getting carbon intensity from Postgres: %s", e)
            raise QueryError(f"Error getting carbon intensity: {e}") from e

    async def get_intensity_point(self, zone: str, timestamp: str) -> Optional[IntensityPoint]:
        """Return the latest stored point at or before ``timestamp`` with its time and flags."""
        ts = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        try:
            async with self.db_manager.connection_scope() as conn:
                row = await conn.fetchrow(
                    f"""
                    SELECT carbon_intensity, datetime, is_estimated, estimation_method
                    FROM carbon_intensity_history
                    WHERE zone = $1 AND datetime <= $2 AND {measured_intensity_sql()}
                    ORDER BY datetime DESC
                    LIMIT 1
                    """,
                    zone,
                    ts,
                )
        except Exception as e:
            logger.error("Error getting carbon intensity point from Postgres: %s", e)
            raise QueryError(f"Error getting carbon intensity point: {e}") from e
        if row is None or row["carbon_intensity"] is None:
            return None
        return IntensityPoint(
            value=float(row["carbon_intensity"]),
            observed_at=row["datetime"],
            is_estimated=row["is_estimated"],
            estimation_method=row["estimation_method"],
        )

    async def save_history(self, history_data: list, zone: str) -> int:
        # Static zone defaults are computed at read time, never persisted (A8).
        history_data = [record for record in history_data or [] if is_persistable_intensity_record(record)]
        if not history_data:
            return 0

        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    INSERT INTO carbon_intensity_history (
                        zone, carbon_intensity, datetime, updated_at, created_at,
                        emission_factor_type, is_estimated, estimation_method
                    ) VALUES (
                        $1, $2, $3, $4, $5, $6, $7, $8
                    )
                    ON CONFLICT (zone, datetime) DO UPDATE SET
                        carbon_intensity = EXCLUDED.carbon_intensity,
                        updated_at = EXCLUDED.updated_at,
                        emission_factor_type = EXCLUDED.emission_factor_type,
                        is_estimated = EXCLUDED.is_estimated,
                        estimation_method = EXCLUDED.estimation_method;
                """

                records = []
                for record in history_data:
                    # Parse timestamp if string, handling Z suffix
                    ts = record.get("datetime")
                    if ts and isinstance(ts, str):
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))

                    # Parse updated_at
                    updated_at = record.get("updatedAt")
                    if updated_at and isinstance(updated_at, str):
                        updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))

                    # Parse created_at
                    created_at = record.get("createdAt")
                    if created_at and isinstance(created_at, str):
                        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))

                    records.append(
                        (
                            zone,
                            record.get("carbonIntensity"),
                            ts,
                            updated_at,
                            created_at,
                            record.get("emissionFactorType"),
                            record.get("isEstimated"),
                            record.get("estimationMethod"),
                        )
                    )

                await conn.executemany(query, records)
                logger.info("Saved %s records to Postgres for zone %s.", len(history_data), zone)
                return len(history_data)
        except Exception as e:
            logger.error("Error saving history to Postgres: %s", e)
            raise QueryError(f"Error saving history: {e}") from e


class PostgresCombinedMetricsRepository(CombinedMetricsRepository, ContainerMetricsRepository):
    """PostgreSQL implementation for combined metrics data storage."""

    def __init__(self, db_manager):
        self.db_manager = db_manager

    async def compression_boundary(self, raw_table: str) -> datetime:
        """Return the compression watermark of ``raw_table``: the raw/hourly read boundary (ROL-01)."""
        from ..raw_hourly_split import read_compression_boundary

        return await read_compression_boundary(self.db_manager, raw_table, f"{raw_table}_hourly")

    @asynccontextmanager
    async def _connection(self, conn: Any = None) -> AsyncIterator[Any]:
        """``conn`` when given (the caller's transaction), else a pooled connection."""
        if conn is not None:
            yield conn
            return
        async with self.db_manager.connection_scope() as scoped:
            yield scoped

    async def _write_window_idle_rows(
        self, conn: Any, idle_rows: List[Any], window: tuple[datetime, datetime], run_id: str
    ) -> None:
        """Idle rows of a window inside its transaction; a failure names them and rolls the window back."""
        try:
            await self.write_idle_metrics(idle_rows, window=window, run_id=run_id, conn=conn)
        except Exception as error:
            raise QueryError(f"Could not store {len(idle_rows)} node idle row(s): {error}") from error

    async def write_window(
        self,
        rows: List[CombinedMetric],
        idle_rows: List[Any],
        *,
        window: tuple[datetime, datetime],
        run_id: str,
        on_commit: Optional[Callable[[Any], Awaitable[Any]]] = None,
        force: bool = False,
    ) -> int:
        """See :meth:`CombinedMetricsRepository.write_window`."""
        with forced_replace(force):
            return await self._write_window(rows, idle_rows, window=window, run_id=run_id, on_commit=on_commit)

    async def _write_window(
        self,
        rows: List[CombinedMetric],
        idle_rows: List[Any],
        *,
        window: tuple[datetime, datetime],
        run_id: str,
        on_commit: Optional[Callable[[Any], Awaitable[Any]]] = None,
    ) -> int:
        async with self.db_manager.connection_scope() as conn:
            async with conn.transaction():
                written = (
                    await self.write_combined_metrics(
                        rows, replace_containers=True, window=window, run_id=run_id, conn=conn
                    )
                    if rows
                    else 0
                )
                await self._write_window_idle_rows(conn, list(idle_rows), window, run_id)
                if on_commit is not None:
                    await on_commit(conn)
        return written

    async def write_combined_metrics(
        self,
        metrics: List[CombinedMetric],
        *,
        replace_containers: bool = False,
        window: Optional[tuple[datetime, datetime]] = None,
        run_id: Optional[str] = None,
        conn: Any = None,
    ) -> int:
        """See :meth:`CombinedMetricsRepository.write_combined_metrics`; on ``conn`` (a savepoint) when given."""
        container_metrics = [metric for metric in metrics if metric.container_name]
        metrics = [metric for metric in metrics if not metric.container_name]
        collected_at = datetime.now(timezone.utc) if run_id else None
        if not metrics:
            if not container_metrics:
                return 0
            return await self.write_container_metrics(container_metrics, conn, run_id=run_id, collected_at=collected_at)
        produced = produced_pod_keys(metrics)

        saved_count = 0
        try:
            async with self._connection(conn) as conn:
                query = """
                    INSERT INTO combined_metrics (
                        pod_name, namespace, total_cost, co2e_grams,
                        pue, grid_intensity, joules, cpu_request,
                        memory_request, cpu_usage_millicores, memory_usage_bytes,
                        network_receive_bytes, network_transmit_bytes,
                        disk_read_bytes, disk_write_bytes,
                        storage_request_bytes, storage_usage_bytes,
                        ephemeral_storage_request_bytes, ephemeral_storage_usage_bytes,
                        gpu_usage_millicores, restart_count,
                        owner_kind, owner_name,
                        period, timestamp,
                        duration_seconds, grid_intensity_timestamp,
                        node, node_instance_type, node_zone,
                        emaps_zone, estimation_reasons, is_estimated, embodied_co2e_grams,
                        calculation_version,
                        cost_cpu, cost_ram, cost_storage, cost_network, cost_lb,
                        cost_source, usage_source, power_source, intensity_source,
                        model_version, gpu_power_watts, node_source, metadata_source, embodied_source, gpu_source,
                        collected_at, collection_run_id, memory_usage_max_bytes
                    ) VALUES (
                        $1, $2, $3, $4,
                        $5, $6, $7, $8,
                        $9, $10, $11,
                        $12, $13,
                        $14, $15,
                        $16, $17,
                        $18, $19,
                        $20, $21,
                        $22, $23,
                        $24, $25,
                        $26, $27,
                        $28, $29, $30,
                        $31, $32, $33, $34,
                        $35,
                        $36, $37, $38, $39, $40,
                        $41, $42, $43, $44,
                        $45, $46, $47, $48,
                        $49, $50, $51, $52, $53
                    )
                    ON CONFLICT (pod_name, namespace, timestamp) DO UPDATE SET
                        co2e_grams = EXCLUDED.co2e_grams,
                        pue = EXCLUDED.pue,
                        grid_intensity = EXCLUDED.grid_intensity,
                        joules = EXCLUDED.joules,
                        cpu_usage_millicores = EXCLUDED.cpu_usage_millicores,
                        memory_usage_bytes = EXCLUDED.memory_usage_bytes,
                        memory_usage_max_bytes = EXCLUDED.memory_usage_max_bytes,
                        network_receive_bytes = EXCLUDED.network_receive_bytes,
                        network_transmit_bytes = EXCLUDED.network_transmit_bytes,
                        disk_read_bytes = EXCLUDED.disk_read_bytes,
                        disk_write_bytes = EXCLUDED.disk_write_bytes,
                        storage_request_bytes = EXCLUDED.storage_request_bytes,
                        storage_usage_bytes = EXCLUDED.storage_usage_bytes,
                        ephemeral_storage_request_bytes = EXCLUDED.ephemeral_storage_request_bytes,
                        ephemeral_storage_usage_bytes = EXCLUDED.ephemeral_storage_usage_bytes,
                        gpu_usage_millicores = EXCLUDED.gpu_usage_millicores,
                        restart_count = EXCLUDED.restart_count,
                        period = EXCLUDED.period,
                        duration_seconds = EXCLUDED.duration_seconds,
                        grid_intensity_timestamp = EXCLUDED.grid_intensity_timestamp,
                        node = EXCLUDED.node,
                        node_instance_type = EXCLUDED.node_instance_type,
                        node_zone = EXCLUDED.node_zone,
                        emaps_zone = EXCLUDED.emaps_zone,
                        estimation_reasons = EXCLUDED.estimation_reasons,
                        is_estimated = EXCLUDED.is_estimated,
                        embodied_co2e_grams = EXCLUDED.embodied_co2e_grams,
                        calculation_version = EXCLUDED.calculation_version,
                        usage_source = EXCLUDED.usage_source,
                        power_source = EXCLUDED.power_source,
                        intensity_source = EXCLUDED.intensity_source,
                        model_version = EXCLUDED.model_version,
                        gpu_power_watts = EXCLUDED.gpu_power_watts,
                        embodied_source = EXCLUDED.embodied_source,
                        gpu_source = EXCLUDED.gpu_source,
                        collected_at = EXCLUDED.collected_at,
                        collection_run_id = EXCLUDED.collection_run_id,
                """
                query += cost_guard_assignments("combined_metrics") + ",\n" + _metadata_upsert("combined_metrics") + ";"

                if replace_containers:
                    complete = complete_container_rows(container_metrics)
                    metrics = await self._without_rows_over_stored_containers(conn, metrics, complete)
                    container_metrics = container_rows_of(metrics, container_metrics)
                metrics_data = []
                for metric in metrics:
                    reasons = metric.estimation_reasons
                    reasons_json = json.dumps(reasons) if reasons else "[]"

                    metrics_data.append(
                        (
                            metric.pod_name,
                            metric.namespace,
                            metric.total_cost,
                            metric.co2e_grams,
                            metric.pue,
                            metric.grid_intensity,
                            metric.joules,
                            metric.cpu_request,
                            metric.memory_request,
                            metric.cpu_usage_millicores,
                            metric.memory_usage_bytes,
                            metric.network_receive_bytes,
                            metric.network_transmit_bytes,
                            metric.disk_read_bytes,
                            metric.disk_write_bytes,
                            metric.storage_request_bytes,
                            metric.storage_usage_bytes,
                            metric.ephemeral_storage_request_bytes,
                            metric.ephemeral_storage_usage_bytes,
                            metric.gpu_usage_millicores,
                            metric.restart_count,
                            metric.owner_kind,
                            metric.owner_name,
                            metric.period,
                            metric.timestamp,
                            metric.duration_seconds,
                            metric.grid_intensity_timestamp,
                            metric.node,
                            metric.node_instance_type,
                            metric.node_zone,
                            metric.emaps_zone,
                            reasons_json,
                            metric.is_estimated,
                            metric.embodied_co2e_grams,
                            metric.calculation_version,
                            *_provenance_values(metric),
                            *_allocation_values(metric),
                            *_row_source_values(metric),
                            collected_at,
                            run_id,
                            metric.memory_usage_max_bytes,
                        )
                    )

                # Container rows are rewritten with their pod rows, in the same
                # transaction, so pod and container rows of a step agree (review 4).
                async with conn.transaction():
                    for offset in range(0, len(metrics_data), _EXECUTEMANY_CHUNK):
                        await conn.executemany(query, metrics_data[offset : offset + _EXECUTEMANY_CHUNK])
                    if replace_containers:
                        await self._delete_stale_container_rows(conn, metrics, container_metrics)
                    saved_count = len(metrics)
                    saved_count += await self.write_container_metrics(
                        container_metrics, conn=conn, run_id=run_id, collected_at=collected_at
                    )
                    if window is not None:
                        await self._delete_rows_not_produced(conn, window, produced)
                logger.info("Saved %s combined metrics to Postgres.", len(metrics))
            record_metric_provenance(metrics)
            if container_metrics:
                record_metric_provenance(container_metrics)
        except Exception as e:
            logger.error("Error writing combined metrics to Postgres: %s", e)
            raise QueryError(f"Error writing combined metrics: {e}") from e
        return saved_count

    @staticmethod
    async def _delete_rows_not_produced(conn: Any, window: tuple[datetime, datetime], produced: set[tuple]) -> int:
        """Delete pod and container rows of ``window`` that the new pass did not produce (STO-01).

        Only steps where the pass produced at least one pod row are touched: a
        step without any row in the pass (no scrape data) keeps what is stored.
        """
        start, end = window
        stored = await conn.fetch(
            "SELECT namespace, pod_name, timestamp FROM combined_metrics WHERE timestamp >= $1 AND timestamp < $2",
            ensure_utc(start),
            ensure_utc(end),
        )
        steps = {key[2] for key in produced}
        stale = [
            (row["namespace"], row["pod_name"], row["timestamp"])
            for row in stored
            if ensure_utc(row["timestamp"]) in steps
            and (row["namespace"], row["pod_name"], ensure_utc(row["timestamp"])) not in produced
        ]
        for table in ("container_metrics", "combined_metrics"):
            if stale:
                await conn.executemany(
                    f"DELETE FROM {table} WHERE namespace = $1 AND pod_name = $2 AND timestamp = $3", stale
                )
        return len(stale)

    @staticmethod
    async def _without_rows_over_stored_containers(
        conn: Any, pod_rows: List[CombinedMetric], container_rows: List[CombinedMetric]
    ) -> List[CombinedMetric]:
        """Drop pod rows of a collected window that have no container row in the batch but stored ones.

        Rewriting such a pod row would leave stored container rows whose
        joules no longer sum to it; the stored pod and container rows stay
        together instead (third review F1).
        """
        with_containers = {
            (row.namespace, row.pod_name, row.timestamp) for row in container_rows if row.timestamp is not None
        }
        candidates = [
            row
            for row in pod_rows
            if row.timestamp is not None and (row.namespace, row.pod_name, row.timestamp) not in with_containers
        ]
        if not candidates:
            return pod_rows
        stored = await conn.fetch(
            """SELECT DISTINCT namespace, pod_name, timestamp FROM container_metrics
            WHERE (namespace, pod_name, timestamp) IN (
                SELECT * FROM unnest($1::text[], $2::text[], $3::timestamptz[])
            )""",
            [row.namespace for row in candidates],
            [row.pod_name for row in candidates],
            [row.timestamp for row in candidates],
        )
        covered = {(r["namespace"], r["pod_name"], r["timestamp"]) for r in stored}
        return [
            row
            for row in pod_rows
            if row.timestamp is None or (row.namespace, row.pod_name, row.timestamp) not in covered
        ]

    @staticmethod
    async def _delete_stale_container_rows(
        conn: Any, pod_rows: List[CombinedMetric], container_rows: List[CombinedMetric]
    ) -> None:
        """Delete container rows of re-written pod rows that the new batch no longer produces.

        Pod rows without metadata (``missing``) are skipped: the stored pod row
        and its containers are kept when they had metadata (review 4).  A pod
        row without any container row in the batch (no container usage in this
        pass, for example a missing container series) keeps its stored
        container rows: they are never deleted for lack of data (second review 1b).
        """
        new_containers: dict[tuple[str, str, datetime], list[str]] = defaultdict(list)
        for row in container_rows:
            if row.timestamp is not None and row.container_name:
                new_containers[(row.namespace, row.pod_name, row.timestamp)].append(row.container_name)
        targets = [
            (
                row.namespace,
                row.pod_name,
                row.timestamp,
                new_containers.get((row.namespace, row.pod_name, row.timestamp), []),
            )
            for row in pod_rows
            if row.timestamp is not None
            and row.metadata_source != "missing"
            and new_containers.get((row.namespace, row.pod_name, row.timestamp))
        ]
        if targets:
            await conn.executemany(
                """DELETE FROM container_metrics
                WHERE namespace = $1 AND pod_name = $2 AND timestamp = $3 AND NOT (container_name = ANY($4::text[]))""",
                targets,
            )

    async def stored_window_profile(self, start: datetime, end: datetime) -> dict[datetime, WindowStepProfile]:
        """See :meth:`CombinedMetricsRepository.stored_window_profile`."""
        try:
            async with self.db_manager.connection_scope() as conn:
                rows = await conn.fetch(
                    """SELECT namespace, pod_name, timestamp, cost_source FROM combined_metrics
                    WHERE timestamp >= $1 AND timestamp < $2""",
                    ensure_utc(start),
                    ensure_utc(end),
                )
        except Exception as e:
            raise QueryError(f"Error reading the stored rows of a window: {e}") from e
        return window_step_profiles((r["namespace"], r["pod_name"], r["timestamp"], r["cost_source"]) for r in rows)

    async def read_combined_metrics(self, start_time: datetime, end_time: datetime) -> List[CombinedMetric]:
        """
        Reads combined metrics from the database within a time range.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    SELECT * FROM combined_metrics
                    WHERE timestamp >= $1 AND timestamp < $2
                    ORDER BY timestamp
                """
                results = await conn.fetch(query, start_time, end_time)

                metrics = []
                for row in results:
                    metric_data = dict(row)

                    # Remove database-only columns not in CombinedMetric model
                    metric_data.pop("id", None)

                    # Deserialize estimation_reasons from JSON string
                    if "estimation_reasons" in metric_data and isinstance(metric_data["estimation_reasons"], str):
                        try:
                            metric_data["estimation_reasons"] = json.loads(metric_data["estimation_reasons"])
                        except json.JSONDecodeError:
                            metric_data["estimation_reasons"] = []

                    metrics.append(CombinedMetric(**metric_data))

                return metrics
        except Exception as e:
            logger.error("Error reading combined metrics from Postgres: %s", e)
            raise QueryError(f"Error reading combined metrics: {e}") from e

    async def read_hourly_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[CombinedMetric]:
        """Read pre-aggregated hourly metrics from the hourly table."""
        try:
            async with self.db_manager.connection_scope() as conn:
                if namespace:
                    query = """
                        SELECT * FROM combined_metrics_hourly
                        WHERE hour_bucket >= $1 AND hour_bucket < $2
                          AND namespace = $3
                        ORDER BY hour_bucket
                    """
                    results = await conn.fetch(query, start_time, end_time, namespace)
                else:
                    query = """
                        SELECT * FROM combined_metrics_hourly
                        WHERE hour_bucket >= $1 AND hour_bucket < $2
                        ORDER BY hour_bucket
                    """
                    results = await conn.fetch(query, start_time, end_time)

                metrics = []
                for row in results:
                    data = dict(row)
                    data.pop("id", None)
                    # Map hourly columns back to CombinedMetric fields
                    data["timestamp"] = data.pop("hour_bucket", None)
                    data["cpu_usage_millicores"] = data.pop("cpu_usage_avg", None)
                    data["cpu_usage_max_millicores"] = data.pop("cpu_usage_max", None)
                    data["memory_usage_bytes"] = data.pop("memory_usage_avg", None)
                    data["memory_usage_max_bytes"] = data.pop("memory_usage_max", None)
                    if "estimation_reasons" in data and isinstance(data["estimation_reasons"], str):
                        try:
                            data["estimation_reasons"] = json.loads(data["estimation_reasons"])
                        except json.JSONDecodeError:
                            data["estimation_reasons"] = []
                    metrics.append(CombinedMetric(**data))
                return metrics
        except Exception as e:
            logger.error("Error reading hourly metrics from Postgres: %s", e)
            raise QueryError(f"Error reading hourly metrics: {e}") from e

    async def write_container_metrics(
        self,
        metrics: List[CombinedMetric],
        conn: Any = None,
        *,
        run_id: Optional[str] = None,
        collected_at: Optional[datetime] = None,
    ) -> int:
        """Upsert container rows; on ``conn`` (inside the caller's transaction) when given."""
        if not metrics:
            return 0

        query = """
            INSERT INTO container_metrics (
                pod_name, namespace, container_name, total_cost, co2e_grams,
                embodied_co2e_grams, pue, grid_intensity, joules,
                cpu_request, memory_request, cpu_usage_millicores, cpu_usage_max_millicores,
                memory_usage_bytes, memory_usage_max_bytes, owner_kind, owner_name,
                timestamp, duration_seconds, is_estimated, estimation_reasons, calculation_version,
                cost_cpu, cost_ram, cost_storage, cost_network, cost_lb,
                cost_source, usage_source, power_source, intensity_source,
                emaps_zone, cpu_limit, memory_limit, model_version, gpu_power_watts,
                node_source, metadata_source, embodied_source, gpu_source, collected_at, collection_run_id
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9,
                $10, $11, $12, $13, $14, $15, $16, $17,
                $18, $19, $20, $21, $22,
                $23, $24, $25, $26, $27, $28, $29, $30, $31,
                $32, $33, $34, $35, $36, $37, $38, $39, $40, $41, $42
            )
            ON CONFLICT (namespace, pod_name, container_name, timestamp) DO UPDATE SET
                co2e_grams = EXCLUDED.co2e_grams,
                embodied_co2e_grams = EXCLUDED.embodied_co2e_grams,
                pue = EXCLUDED.pue,
                grid_intensity = EXCLUDED.grid_intensity,
                joules = EXCLUDED.joules,
                cpu_request = EXCLUDED.cpu_request,
                memory_request = EXCLUDED.memory_request,
                cpu_usage_millicores = EXCLUDED.cpu_usage_millicores,
                cpu_usage_max_millicores = EXCLUDED.cpu_usage_max_millicores,
                memory_usage_bytes = EXCLUDED.memory_usage_bytes,
                memory_usage_max_bytes = EXCLUDED.memory_usage_max_bytes,
                owner_kind = EXCLUDED.owner_kind,
                owner_name = EXCLUDED.owner_name,
                duration_seconds = EXCLUDED.duration_seconds,
                is_estimated = EXCLUDED.is_estimated,
                estimation_reasons = EXCLUDED.estimation_reasons,
                calculation_version = EXCLUDED.calculation_version,
                usage_source = EXCLUDED.usage_source,
                power_source = EXCLUDED.power_source,
                intensity_source = EXCLUDED.intensity_source,
                emaps_zone = EXCLUDED.emaps_zone,
                cpu_limit = EXCLUDED.cpu_limit,
                memory_limit = EXCLUDED.memory_limit,
                model_version = EXCLUDED.model_version,
                gpu_power_watts = EXCLUDED.gpu_power_watts,
                embodied_source = EXCLUDED.embodied_source,
                gpu_source = EXCLUDED.gpu_source,
                node_source = EXCLUDED.node_source,
                metadata_source = EXCLUDED.metadata_source,
                collected_at = EXCLUDED.collected_at,
                collection_run_id = EXCLUDED.collection_run_id,
        """ + cost_guard_assignments("container_metrics")
        rows = []
        for metric in metrics:
            container_name = metric.container_name
            timestamp = metric.timestamp
            if not container_name or timestamp is None:
                raise ValueError("container metrics require a container name and timestamp")
            rows.append(
                (
                    metric.pod_name,
                    metric.namespace,
                    container_name,
                    metric.total_cost,
                    metric.co2e_grams,
                    metric.embodied_co2e_grams,
                    metric.pue,
                    metric.grid_intensity,
                    metric.joules,
                    metric.cpu_request,
                    metric.memory_request,
                    metric.cpu_usage_millicores,
                    metric.cpu_usage_max_millicores,
                    metric.memory_usage_bytes,
                    metric.memory_usage_max_bytes,
                    metric.owner_kind,
                    metric.owner_name,
                    timestamp,
                    metric.duration_seconds,
                    metric.is_estimated,
                    json.dumps(metric.estimation_reasons) if metric.estimation_reasons else "[]",
                    metric.calculation_version,
                    *_provenance_values(metric),
                    metric.emaps_zone,
                    metric.cpu_limit,
                    metric.memory_limit,
                    *_allocation_values(metric),
                    *_row_source_values(metric),
                    collected_at,
                    run_id,
                )
            )
        if conn is not None:
            for offset in range(0, len(rows), _EXECUTEMANY_CHUNK):
                await conn.executemany(query, rows[offset : offset + _EXECUTEMANY_CHUNK])
            return len(rows)
        try:
            async with self.db_manager.connection_scope() as scoped:
                async with scoped.transaction():
                    for offset in range(0, len(rows), _EXECUTEMANY_CHUNK):
                        await scoped.executemany(query, rows[offset : offset + _EXECUTEMANY_CHUNK])
            record_metric_provenance(metrics)
            return len(rows)
        except Exception as e:
            logger.error("Error writing container metrics to Postgres: %s", e)
            raise QueryError(f"Error writing container metrics: {e}") from e

    async def write_idle_metrics(
        self,
        metrics: List[Any],
        *,
        window: Optional[tuple[datetime, datetime]] = None,
        run_id: Optional[str] = None,
        conn: Any = None,
    ) -> int:
        """Upserts ``__idle__`` rows (one per node and step) into ``node_idle_metrics`` (B2).

        With ``window``, idle rows of the window's steps that the pass did not
        produce (a node gone since the first pass) are deleted (STO-01). On
        ``conn`` (inside the caller's transaction) when given.
        """
        if not metrics:
            return 0
        columns = IDLE_METRIC_COLUMNS
        placeholders = ", ".join(f"${index}" for index in range(1, len(columns) + 3))
        updates = ", ".join(
            f"{column} = EXCLUDED.{column}"
            for column in columns
            if column not in ("node", "timestamp") and column not in IDLE_COST_COLUMNS
        )
        updates += ", collected_at = EXCLUDED.collected_at, collection_run_id = EXCLUDED.collection_run_id"
        updates += ", " + cost_guard_assignments("node_idle_metrics", IDLE_COST_COLUMNS)
        query = (
            f"INSERT INTO node_idle_metrics ({', '.join(columns)}, collected_at, collection_run_id) "
            f"VALUES ({placeholders}) ON CONFLICT (node, timestamp) DO UPDATE SET {updates}"
        )
        collected_at = datetime.now(timezone.utc) if run_id else None
        rows = [(*(getattr(metric, column) for column in columns), collected_at, run_id) for metric in metrics]

        async def write(connection: Any) -> None:
            await connection.executemany(query, rows)
            if window is not None:
                await self._delete_idle_not_produced(connection, window, produced_idle_keys(metrics))

        try:
            if conn is not None:
                await write(conn)
            else:
                async with self.db_manager.connection_scope() as scoped:
                    async with scoped.transaction():
                        await write(scoped)
        except Exception as e:
            logger.error("Error writing node idle metrics to Postgres: %s", e)
            raise QueryError(f"Error writing node idle metrics: {e}") from e
        return len(rows)

    @staticmethod
    async def _delete_idle_not_produced(conn: Any, window: tuple[datetime, datetime], produced: set[tuple]) -> int:
        """Delete idle rows of the pass's steps in ``window`` whose node the pass did not produce (STO-01)."""
        start, end = window
        stored = await conn.fetch(
            "SELECT node, timestamp FROM node_idle_metrics WHERE timestamp >= $1 AND timestamp < $2",
            ensure_utc(start),
            ensure_utc(end),
        )
        steps = {key[1] for key in produced}
        stale = [
            (row["node"], row["timestamp"])
            for row in stored
            if ensure_utc(row["timestamp"]) in steps and (row["node"], ensure_utc(row["timestamp"])) not in produced
        ]
        if stale:
            await conn.executemany("DELETE FROM node_idle_metrics WHERE node = $1 AND timestamp = $2", stale)
        return len(stale)

    async def read_idle_metrics(self, start_time: datetime, end_time: datetime) -> List[Any]:
        """Reads ``__idle__`` rows whose timestamp is in ``[start_time, end_time)``."""
        from greenkube.impact.rows import NodeIdleMetric

        try:
            async with self.db_manager.connection_scope() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT {", ".join(IDLE_METRIC_COLUMNS)} FROM node_idle_metrics
                    WHERE timestamp >= $1 AND timestamp < $2
                    ORDER BY timestamp, node
                    """,
                    start_time,
                    end_time,
                )
            return [NodeIdleMetric(**dict(row)) for row in rows]
        except Exception as e:
            logger.error("Error reading node idle metrics from Postgres: %s", e)
            raise QueryError(f"Error reading node idle metrics: {e}") from e

    async def read_latest_idle_gpu_sources(self, start_time: datetime, end_time: datetime) -> Dict[str, Optional[str]]:
        """``{node: gpu_source}`` of each node's latest idle row in ``[start_time, end_time)`` (NRG-13)."""
        try:
            async with self.db_manager.connection_scope() as conn:
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT ON (node) node, gpu_source FROM node_idle_metrics
                    WHERE timestamp >= $1 AND timestamp < $2
                    ORDER BY node, timestamp DESC
                    """,
                    start_time,
                    end_time,
                )
            return {row["node"]: row["gpu_source"] for row in rows}
        except Exception as e:
            logger.error("Error reading node GPU power sources from Postgres: %s", e)
            raise QueryError(f"Error reading node GPU power sources: {e}") from e

    async def _idle_parts(self, start_time: datetime, end_time: datetime, ts_raw: str, ts_hourly: str):
        """UNION ALL branches of the idle rows of a window, split at the idle compression watermark."""
        split = await self._raw_hourly_split(start_time, end_time, "node_idle_metrics")
        columns = "co2e_grams, embodied_co2e_grams, total_cost, joules"
        parts: list[str] = []
        params: list[Any] = []
        if split.has_raw:
            first = len(params) + 1
            parts.append(
                f"SELECT {ts_raw} AS ts, {columns} FROM node_idle_metrics "
                f"WHERE timestamp >= ${first} AND timestamp < ${first + 1} "
                f"AND NOT (timestamp >= ${first + 2} AND timestamp < ${first + 3})"
            )
            params.extend(split.raw_params())
        if split.has_hourly:
            first = len(params) + 1
            parts.append(
                f"SELECT {ts_hourly} AS ts, {columns} FROM node_idle_metrics_hourly "
                f"WHERE hour_bucket >= ${first} AND hour_bucket < ${first + 1}"
            )
            params.extend(split.hourly_params())
        return parts, params

    async def idle_data_since(self) -> Optional[datetime]:
        """First instant covered by a node idle row, raw or hourly; ``None`` when none is stored (NRG-02)."""
        try:
            async with self.db_manager.connection_scope() as conn:
                value = await conn.fetchval(
                    "SELECT LEAST((SELECT MIN(timestamp) FROM node_idle_metrics), "
                    "(SELECT MIN(hour_bucket) FROM node_idle_metrics_hourly))"
                )
            return value.astimezone(timezone.utc) if value is not None else None
        except Exception as e:
            logger.error("idle_data_since failed: %s", e)
            raise QueryError(f"idle_data_since failed: {e}") from e

    async def aggregate_idle_summary(self, start_time: datetime, end_time: datetime) -> Optional[dict]:
        """Totals of the node ``__idle__`` share in ``[start, end)`` (NRG-02), raw and hourly rows.

        ``None`` when the window holds no idle row (periods before idle rows were
        stored, or no data): unknown, never zero.
        """
        try:
            parts, params = await self._idle_parts(start_time, end_time, "NULL::timestamptz", "NULL::timestamptz")
            if not parts:
                return None
            async with self.db_manager.connection_scope() as conn:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) AS n, COALESCE(SUM(co2e_grams), 0) AS co2e, "
                    "COALESCE(SUM(embodied_co2e_grams), 0) AS embodied, COALESCE(SUM(total_cost), 0) AS cost, "
                    f"COALESCE(SUM(joules), 0) AS joules FROM ({' UNION ALL '.join(parts)}) AS idle",
                    *params,
                )
            if not row or not row["n"]:
                return None
            return {
                "idle_co2e_grams": float(row["co2e"]),
                "idle_embodied_co2e_grams": float(row["embodied"]),
                "idle_cost": float(row["cost"]),
                "idle_energy_joules": float(row["joules"]),
            }
        except Exception as e:
            logger.error("Error in aggregate_idle_summary: %s", e)
            raise QueryError(f"aggregate_idle_summary failed: {e}") from e

    async def aggregate_idle_timeseries(
        self, start_time: datetime, end_time: datetime, granularity: str = "hour"
    ) -> List[dict]:
        """Idle share per bucket, same buckets and labels as :meth:`aggregate_timeseries` (NRG-02)."""
        unit = granularity if granularity in ("hour", "day", "week", "month") else "hour"
        formats = {
            "hour": "%Y-%m-%dT%H:00:00Z",
            "day": "%Y-%m-%dT00:00:00Z",
            "week": "%Y-%m-%dT00:00:00Z",
            "month": "%Y-%m-01T00:00:00Z",
        }
        try:
            parts, params = await self._idle_parts(start_time, end_time, "timestamp", "hour_bucket")
            if not parts:
                return []
            async with self.db_manager.connection_scope() as conn:
                rows = await conn.fetch(
                    f"SELECT {_utc_trunc(unit, 'ts')} AS ts_bucket, SUM(co2e_grams) AS co2e, "
                    "SUM(embodied_co2e_grams) AS embodied, SUM(total_cost) AS cost, SUM(joules) AS joules "
                    f"FROM ({' UNION ALL '.join(parts)}) AS idle GROUP BY ts_bucket ORDER BY ts_bucket",
                    *params,
                )
            return [
                {
                    "timestamp": row["ts_bucket"].astimezone(timezone.utc).strftime(formats[unit]),
                    "co2e_grams": float(row["co2e"] or 0.0),
                    "embodied_co2e_grams": float(row["embodied"] or 0.0),
                    "total_cost": float(row["cost"] or 0.0),
                    "energy_joules": float(row["joules"] or 0.0),
                }
                for row in rows
            ]
        except Exception as e:
            logger.error("Error in aggregate_idle_timeseries: %s", e)
            raise QueryError(f"aggregate_idle_timeseries failed: {e}") from e

    async def read_container_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[CombinedMetric]:
        try:
            async with self.db_manager.connection_scope() as conn:
                if namespace:
                    rows = await conn.fetch(
                        """
                        SELECT * FROM container_metrics
                        WHERE timestamp >= $1 AND timestamp < $2 AND namespace = $3
                        ORDER BY timestamp
                        """,
                        start_time,
                        end_time,
                        namespace,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT * FROM container_metrics
                        WHERE timestamp >= $1 AND timestamp < $2
                        ORDER BY timestamp
                        """,
                        start_time,
                        end_time,
                    )
            return [_container_metric_from_row(row, timestamp_column="timestamp") for row in rows]
        except Exception as e:
            logger.error("Error reading container metrics from Postgres: %s", e)
            raise QueryError(f"Error reading container metrics: {e}") from e

    async def read_hourly_container_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[CombinedMetric]:
        try:
            async with self.db_manager.connection_scope() as conn:
                if namespace:
                    rows = await conn.fetch(
                        """
                        SELECT * FROM container_metrics_hourly
                        WHERE hour_bucket >= $1 AND hour_bucket < $2 AND namespace = $3
                        ORDER BY hour_bucket
                        """,
                        start_time,
                        end_time,
                        namespace,
                    )
                else:
                    rows = await conn.fetch(
                        """
                        SELECT * FROM container_metrics_hourly
                        WHERE hour_bucket >= $1 AND hour_bucket < $2
                        ORDER BY hour_bucket
                        """,
                        start_time,
                        end_time,
                    )
            return [_container_metric_from_row(row, timestamp_column="hour_bucket", hourly=True) for row in rows]
        except Exception as e:
            logger.error("Error reading hourly container metrics from Postgres: %s", e)
            raise QueryError(f"Error reading hourly container metrics: {e}") from e

    async def list_metric_years(self, namespace: Optional[str] = None) -> List[int]:
        """Return distinct metric years from raw and hourly Postgres tables."""
        try:
            async with self.db_manager.connection_scope() as conn:
                if namespace:
                    query = """
                        SELECT DISTINCT year FROM (
                            SELECT EXTRACT(YEAR FROM timestamp AT TIME ZONE 'UTC')::int AS year
                            FROM combined_metrics
                            WHERE timestamp IS NOT NULL AND namespace = $1
                            UNION
                            SELECT EXTRACT(YEAR FROM hour_bucket AT TIME ZONE 'UTC')::int AS year
                            FROM combined_metrics_hourly
                            WHERE hour_bucket IS NOT NULL AND namespace = $1
                        ) AS metric_years
                        WHERE year IS NOT NULL
                        ORDER BY year DESC
                    """
                    rows = await conn.fetch(query, namespace)
                else:
                    query = """
                        SELECT DISTINCT year FROM (
                            SELECT EXTRACT(YEAR FROM timestamp AT TIME ZONE 'UTC')::int AS year
                            FROM combined_metrics
                            WHERE timestamp IS NOT NULL
                            UNION
                            SELECT EXTRACT(YEAR FROM hour_bucket AT TIME ZONE 'UTC')::int AS year
                            FROM combined_metrics_hourly
                            WHERE hour_bucket IS NOT NULL
                        ) AS metric_years
                        WHERE year IS NOT NULL
                        ORDER BY year DESC
                    """
                    rows = await conn.fetch(query)
                return [int(row["year"]) for row in rows]
        except Exception as e:
            logger.error("Error listing metric years from Postgres: %s", e)
            raise QueryError(f"Error listing metric years: {e}") from e

    async def aggregate_summary(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> dict:
        """SQL-level aggregation for summary — queries both raw and hourly tables."""

        split = await self._raw_hourly_split(start_time, end_time, "combined_metrics")

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                # Raw table — only for data within the compression window
                if split.has_raw:
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 4}"
                    parts.append(f"""
                        SELECT co2e_grams, embodied_co2e_grams, total_cost, joules,
                               pod_name, namespace
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp < ${idx + 1}
                          AND NOT (timestamp >= ${idx + 2} AND timestamp < ${idx + 3}){ns_clause}
                    """)
                    params.extend(split.raw_params())
                    idx += 4
                    if namespace:
                        params.append(namespace)
                        idx += 1

                # Hourly table — only for data before the compression cutoff
                if split.has_hourly:
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT co2e_grams, embodied_co2e_grams, total_cost, joules,
                               pod_name, namespace
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket < ${idx + 1}{ns_clause}
                    """)
                    params.extend(split.hourly_params())
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return {
                        "total_co2e_grams": 0.0,
                        "total_embodied_co2e_grams": 0.0,
                        "total_cost": 0.0,
                        "total_energy_joules": 0.0,
                        "pod_count": 0,
                        "namespace_count": 0,
                        "row_count": 0,
                    }

                union_query = " UNION ALL ".join(parts)
                query = f"""
                    SELECT
                        COALESCE(SUM(co2e_grams), 0)          AS total_co2e,
                        COALESCE(SUM(embodied_co2e_grams), 0) AS total_embodied,
                        COALESCE(SUM(total_cost), 0)          AS total_cost,
                        COALESCE(SUM(joules), 0)              AS total_energy,
                        COUNT(DISTINCT pod_name)               AS pod_count,
                        COUNT(DISTINCT namespace)              AS namespace_count,
                        COUNT(*)                               AS row_count
                    FROM ({union_query}) AS combined
                """
                row = await conn.fetchrow(query, *params)

                return {
                    "total_co2e_grams": row["total_co2e"] or 0.0,
                    "total_embodied_co2e_grams": row["total_embodied"] or 0.0,
                    "total_cost": row["total_cost"] or 0.0,
                    "total_energy_joules": row["total_energy"] or 0.0,
                    "pod_count": row["pod_count"] or 0,
                    "namespace_count": row["namespace_count"] or 0,
                    "row_count": row["row_count"] or 0,
                }
        except Exception as e:
            logger.error("Error in aggregate_summary: %s", e)
            raise QueryError(f"aggregate_summary failed: {e}") from e

    async def aggregate_grouped_row_count(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
        granularity: Optional[str] = None,
        group_by: str = "pod",
    ) -> int:
        """SQL COUNT of distinct groups for a grouped report — no rows loaded.

        Computes COUNT(DISTINCT (group_key, time_bucket)) in the DB so the
        caller never materialises any CombinedMetric objects.

        Handles the raw/hourly split the same way as aggregate_summary.
        """

        # Map report granularity names → PostgreSQL date_trunc units
        _GRAN_TRUNC = {
            "hourly": "hour",
            "daily": "day",
            "weekly": "week",
            "monthly": "month",
            "yearly": "year",
        }

        split = await self._raw_hourly_split(start_time, end_time, "combined_metrics")

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                # Build the group expression common to both tables
                if group_by == "namespace":
                    group_cols = "namespace"
                else:
                    group_cols = "namespace, pod_name"

                trunc_unit = _GRAN_TRUNC.get(granularity or "", None)
                if trunc_unit:
                    # Group by identity cols + time bucket
                    raw_ts_expr = _utc_trunc(trunc_unit, "timestamp")
                    hourly_ts_expr = _utc_trunc(trunc_unit, "hour_bucket")
                else:
                    raw_ts_expr = "NULL::timestamptz"
                    hourly_ts_expr = "NULL::timestamptz"

                if split.has_raw:
                    ns_clause = f" AND namespace = ${idx + 4}" if namespace else ""
                    parts.append(f"""
                        SELECT {group_cols}, {raw_ts_expr} AS ts_bucket
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp < ${idx + 1}
                          AND NOT (timestamp >= ${idx + 2} AND timestamp < ${idx + 3}){ns_clause}
                    """)
                    params.extend(split.raw_params())
                    idx += 4
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if split.has_hourly:
                    ns_clause = f" AND namespace = ${idx + 2}" if namespace else ""
                    parts.append(f"""
                        SELECT {group_cols}, {hourly_ts_expr} AS ts_bucket
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket < ${idx + 1}{ns_clause}
                    """)
                    params.extend(split.hourly_params())
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return 0

                union_cte = " UNION ALL ".join(parts)
                if group_by == "namespace":
                    distinct_cols = "namespace, ts_bucket"
                else:
                    distinct_cols = "namespace, pod_name, ts_bucket"

                query = f"""
                    SELECT COUNT(*) AS group_count
                    FROM (
                        SELECT DISTINCT {distinct_cols}
                        FROM ({union_cte}) AS all_rows
                    ) AS groups
                """
                row = await conn.fetchrow(query, *params)
                return int(row["group_count"]) if row else 0
        except Exception as e:
            logger.error("Error in aggregate_grouped_row_count: %s", e)
            raise QueryError(f"aggregate_grouped_row_count failed: {e}") from e

    async def aggregate_timeseries(
        self,
        start_time: datetime,
        end_time: datetime,
        granularity: str = "hour",
        namespace: Optional[str] = None,
    ) -> list:
        """SQL-level time-series aggregation — queries both raw and hourly tables."""

        _PG_TRUNC = {
            "hour": "hour",
            "day": "day",
            "week": "week",
            "month": "month",
        }
        trunc_unit = _PG_TRUNC.get(granularity, "hour")

        split = await self._raw_hourly_split(start_time, end_time, "combined_metrics")

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                # Raw table — recent data
                if split.has_raw:
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 4}"
                    parts.append(f"""
                        SELECT timestamp AS ts, co2e_grams, embodied_co2e_grams,
                               total_cost, joules, cpu_usage_millicores, memory_usage_bytes
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp < ${idx + 1}
                          AND NOT (timestamp >= ${idx + 2} AND timestamp < ${idx + 3}){ns_clause}
                    """)
                    params.extend(split.raw_params())
                    idx += 4
                    if namespace:
                        params.append(namespace)
                        idx += 1

                # Hourly table — old data
                if split.has_hourly:
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT hour_bucket AS ts, co2e_grams, embodied_co2e_grams,
                               total_cost, joules, cpu_usage_avg AS cpu_usage_millicores,
                               memory_usage_avg AS memory_usage_bytes
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket < ${idx + 1}{ns_clause}
                    """)
                    params.extend(split.hourly_params())
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return []

                union_query = " UNION ALL ".join(parts)
                query = f"""
                    SELECT
                        {_utc_trunc(trunc_unit, "ts")} AS ts_bucket,
                        COALESCE(SUM(co2e_grams), 0)          AS co2e_grams,
                        COALESCE(SUM(embodied_co2e_grams), 0) AS embodied_co2e_grams,
                        COALESCE(SUM(total_cost), 0)          AS total_cost,
                        COALESCE(SUM(joules), 0)              AS energy_joules,
                        SUM(cpu_usage_millicores) AS cpu_usage_millicores,
                        SUM(memory_usage_bytes) AS memory_usage_bytes
                    FROM ({union_query}) AS combined
                    GROUP BY ts_bucket
                    ORDER BY ts_bucket
                """
                rows = await conn.fetch(query, *params)

                _FORMATS = {
                    "hour": "%Y-%m-%dT%H:00:00Z",
                    "day": "%Y-%m-%dT00:00:00Z",
                    "week": "%Y-%m-%dT00:00:00Z",  # date_trunc('week') → Monday
                    "month": "%Y-%m-01T00:00:00Z",
                }
                fmt = _FORMATS.get(granularity, "%Y-%m-%dT%H:00:00Z")
                return [
                    {
                        "timestamp": row["ts_bucket"].strftime(fmt) if row["ts_bucket"] else "",
                        "co2e_grams": row["co2e_grams"],
                        "embodied_co2e_grams": row["embodied_co2e_grams"],
                        "total_cost": row["total_cost"],
                        "energy_joules": row["energy_joules"],
                        "cpu_usage_millicores": row["cpu_usage_millicores"],
                        "memory_usage_bytes": row["memory_usage_bytes"],
                    }
                    for row in rows
                ]
        except Exception as e:
            logger.error("Error in aggregate_timeseries: %s", e)
            raise QueryError(f"aggregate_timeseries failed: {e}") from e

    async def list_namespaces(self) -> list:
        """Return namespaces from the cache table (fast, no table scan)."""
        try:
            async with self.db_manager.connection_scope() as conn:
                rows = await conn.fetch("SELECT namespace FROM namespace_cache ORDER BY namespace")
                if rows:
                    return [row["namespace"] for row in rows]
                # Fallback: scan recent combined_metrics if cache is empty
                rows = await conn.fetch(
                    """
                    SELECT DISTINCT namespace
                    FROM combined_metrics
                    WHERE timestamp > NOW() - INTERVAL '7 days'
                    ORDER BY namespace
                    """
                )
                return [row["namespace"] for row in rows]
        except Exception as e:
            logger.warning("list_namespaces failed, returning empty list: %s", e)
            return []

    async def aggregate_by_namespace(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> list:
        """SQL-level aggregation of metrics grouped by namespace."""

        split = await self._raw_hourly_split(start_time, end_time, "combined_metrics")

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                if split.has_raw:
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 4}"
                    parts.append(f"""
                        SELECT namespace, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp < ${idx + 1}
                          AND NOT (timestamp >= ${idx + 2} AND timestamp < ${idx + 3}){ns_clause}
                    """)
                    params.extend(split.raw_params())
                    idx += 4
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if split.has_hourly:
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT namespace, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket < ${idx + 1}{ns_clause}
                    """)
                    params.extend(split.hourly_params())
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return []

                union_query = " UNION ALL ".join(parts)
                query = f"""
                    SELECT
                        namespace,
                        COALESCE(SUM(co2e_grams), 0)          AS co2e_grams,
                        COALESCE(SUM(embodied_co2e_grams), 0) AS embodied_co2e_grams,
                        COALESCE(SUM(total_cost), 0)           AS total_cost,
                        COALESCE(SUM(joules), 0)               AS energy_joules
                    FROM ({union_query}) AS combined
                    GROUP BY namespace
                    ORDER BY co2e_grams DESC
                """
                rows = await conn.fetch(query, *params)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Error in aggregate_by_namespace: %s", e)
            raise QueryError(f"aggregate_by_namespace failed: {e}") from e

    async def aggregate_top_pods(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
        limit: int = 10,
    ) -> list:
        """SQL-level aggregation of top pods by CO2 emissions."""

        split = await self._raw_hourly_split(start_time, end_time, "combined_metrics")

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []
                idx = 1

                if split.has_raw:
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 4}"
                    parts.append(f"""
                        SELECT namespace, pod_name, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp < ${idx + 1}
                          AND NOT (timestamp >= ${idx + 2} AND timestamp < ${idx + 3}){ns_clause}
                    """)
                    params.extend(split.raw_params())
                    idx += 4
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if split.has_hourly:
                    ns_clause = ""
                    if namespace:
                        ns_clause = f" AND namespace = ${idx + 2}"
                    parts.append(f"""
                        SELECT namespace, pod_name, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket < ${idx + 1}{ns_clause}
                    """)
                    params.extend(split.hourly_params())
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return []

                union_query = " UNION ALL ".join(parts)
                query = f"""
                    SELECT
                        namespace,
                        pod_name,
                        COALESCE(SUM(co2e_grams), 0)          AS co2e_grams,
                        COALESCE(SUM(embodied_co2e_grams), 0) AS embodied_co2e_grams,
                        COALESCE(SUM(total_cost), 0)           AS total_cost,
                        COALESCE(SUM(joules), 0)               AS energy_joules
                    FROM ({union_query}) AS combined
                    GROUP BY namespace, pod_name
                    ORDER BY co2e_grams DESC
                    LIMIT ${idx}
                """
                params.append(limit)
                rows = await conn.fetch(query, *params)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error("Error in aggregate_top_pods: %s", e)
            raise QueryError(f"aggregate_top_pods failed: {e}") from e

    async def read_combined_metrics_cursor(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
        cursor: Optional[dict] = None,
        limit: int = 1000,
    ) -> tuple[int, List[CombinedMetric], Optional[dict]]:
        """Read a bounded, stable page ordered by timestamp and pod identity."""
        limit = min(max(limit, 1), 1000)
        try:
            async with self.db_manager.connection_scope() as conn:
                filters = ["timestamp >= $1", "timestamp < $2"]
                params: list = [start_time, end_time]
                idx = 3
                if namespace:
                    filters.append(f"namespace = ${idx}")
                    params.append(namespace)
                    idx += 1
                count_where = " AND ".join(filters)
                if cursor:
                    filters.append(
                        f"(timestamp > ${idx} OR (timestamp = ${idx} AND "
                        f"(namespace > ${idx + 1} OR (namespace = ${idx + 1} AND pod_name > ${idx + 2})))"
                    )
                    params.extend(
                        [
                            datetime.fromisoformat(cursor["timestamp"].replace("Z", "+00:00")),
                            cursor["namespace"],
                            cursor["pod_name"],
                        ]
                    )
                    idx += 3
                where = " AND ".join(filters)
                count_params = params[: idx - (3 if cursor else 0)]
                total = await conn.fetchval(f"SELECT COUNT(*) FROM combined_metrics WHERE {count_where}", *count_params)
                rows = await conn.fetch(
                    f"SELECT * FROM combined_metrics WHERE {where} "
                    f"ORDER BY timestamp, namespace, pod_name LIMIT ${idx}",
                    *params,
                    limit + 1,
                )
                metrics = []
                for row in rows[:limit]:
                    data = dict(row)
                    data.pop("id", None)
                    if isinstance(data.get("estimation_reasons"), str):
                        data["estimation_reasons"] = json.loads(data["estimation_reasons"])
                    metrics.append(CombinedMetric(**data))
                next_cursor = None
                if len(rows) > limit and metrics:
                    last = metrics[-1]
                    assert last.timestamp is not None
                    next_cursor = {
                        "timestamp": last.timestamp.isoformat().replace("+00:00", "Z"),
                        "namespace": last.namespace,
                        "pod_name": last.pod_name,
                    }
                return int(total or 0), metrics, next_cursor
        except (ValueError, KeyError, TypeError) as e:
            logger.error("Error in cursor-paginated combined metrics: %s", e)
            raise QueryError(f"Cursor pagination failed: {e}") from e

    async def count_combined_metrics_smart(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> Optional[int]:
        """Count raw and hourly combined rows without materializing them."""
        return await self._count_smart("combined_metrics", start_time, end_time, namespace)

    async def count_container_metrics_smart(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> Optional[int]:
        """Count raw and hourly container rows without materializing them."""
        return await self._count_smart("container_metrics", start_time, end_time, namespace)

    async def _count_smart(
        self, table: str, start_time: datetime, end_time: datetime, namespace: Optional[str]
    ) -> Optional[int]:
        """Count the rows of ``table`` and its hourly table split at the compression watermark (ROL-01)."""
        split = await self._raw_hourly_split(start_time, end_time, table)
        ns_raw = " AND namespace = $5" if namespace else ""
        ns_hourly = " AND namespace = $3" if namespace else ""
        extra = [namespace] if namespace else []
        try:
            async with self.db_manager.connection_scope() as conn:
                total = 0
                if split.has_raw:
                    total += int(
                        await conn.fetchval(
                            f"SELECT COUNT(*) FROM {table} WHERE timestamp >= $1 AND timestamp < $2 "
                            f"AND NOT (timestamp >= $3 AND timestamp < $4){ns_raw}",
                            *split.raw_params(),
                            *extra,
                        )
                        or 0
                    )
                if split.has_hourly:
                    total += int(
                        await conn.fetchval(
                            f"SELECT COUNT(*) FROM {table}_hourly "
                            f"WHERE hour_bucket >= $1 AND hour_bucket < $2{ns_hourly}",
                            *split.hourly_params(),
                            *extra,
                        )
                        or 0
                    )
                return total
        except Exception as exc:
            logger.warning("Could not count %s: %s", table, exc)
            return None

    async def read_combined_metrics_page(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
        offset: int = 0,
        limit: int = 1000,
    ) -> tuple[int, List[CombinedMetric]]:
        """DB-level paginated read: COUNT(total) + LIMIT/OFFSET in a single round-trip.

        Handles the raw/hourly split:
        - Data at or after the compression watermark: queries combined_metrics.
        - Older data: queries combined_metrics_hourly (ROL-01).
        - Mixed ranges: UNION ALL with column normalization.

        Returns:
            Tuple of (total_count, page_items).
        """

        split = await self._raw_hourly_split(start_time, end_time, "combined_metrics")

        try:
            async with self.db_manager.connection_scope() as conn:
                # --- Build UNION ALL parts with normalized columns ---
                # Both tables expose the same set of fields; the hourly table
                # renames a few columns (hour_bucket → timestamp, cpu/mem avg → usage).
                parts: list[str] = []
                params: list = []
                idx = 1

                if split.has_raw:
                    ns_clause = f" AND namespace = ${idx + 4}" if namespace else ""
                    parts.append(f"""
                        SELECT
                            pod_name, namespace, total_cost, co2e_grams, pue,
                            grid_intensity, joules, cpu_request, memory_request,
                            cpu_usage_millicores, memory_usage_bytes,
                            network_receive_bytes, network_transmit_bytes,
                            disk_read_bytes, disk_write_bytes,
                            storage_request_bytes, storage_usage_bytes,
                            ephemeral_storage_request_bytes, ephemeral_storage_usage_bytes,
                            gpu_usage_millicores, restart_count,
                            owner_kind, owner_name, period, timestamp,
                            duration_seconds, grid_intensity_timestamp,
                            node, node_instance_type, node_zone,
                            emaps_zone, estimation_reasons, is_estimated,
                            embodied_co2e_grams, calculation_version,
                            cost_cpu, cost_ram, cost_storage, cost_network, cost_lb,
                            cost_source, usage_source, power_source, intensity_source
                        FROM combined_metrics
                        WHERE timestamp >= ${idx} AND timestamp < ${idx + 1}
                          AND NOT (timestamp >= ${idx + 2} AND timestamp < ${idx + 3}){ns_clause}
                    """)
                    params.extend(split.raw_params())
                    idx += 4
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if split.has_hourly:
                    ns_clause = f" AND namespace = ${idx + 2}" if namespace else ""
                    parts.append(f"""
                        SELECT
                            pod_name, namespace, total_cost, co2e_grams, pue,
                            grid_intensity, joules, cpu_request, memory_request,
                            cpu_usage_avg AS cpu_usage_millicores,
                            memory_usage_avg AS memory_usage_bytes,
                            network_receive_bytes, network_transmit_bytes,
                            disk_read_bytes, disk_write_bytes,
                            storage_request_bytes, storage_usage_bytes,
                            -- Columns that only exist on the raw table; typed NULLs
                            -- keep the UNION branch shape identical to the raw SELECT.
                            NULL::bigint AS ephemeral_storage_request_bytes,
                            NULL::bigint AS ephemeral_storage_usage_bytes,
                            gpu_usage_millicores, restart_count,
                            owner_kind, owner_name, NULL::text AS period, hour_bucket AS timestamp,
                            duration_seconds, NULL::timestamptz AS grid_intensity_timestamp,
                            node, node_instance_type, node_zone,
                            emaps_zone, estimation_reasons, is_estimated,
                            embodied_co2e_grams, calculation_version,
                            cost_cpu, cost_ram, cost_storage, cost_network, cost_lb,
                            cost_source, usage_source, power_source, intensity_source
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ${idx} AND hour_bucket < ${idx + 1}{ns_clause}
                    """)
                    params.extend(split.hourly_params())
                    idx += 2
                    if namespace:
                        params.append(namespace)
                        idx += 1

                if not parts:
                    return 0, []

                union_cte = " UNION ALL ".join(parts)

                # COUNT and paged SELECT in a single CTE to avoid two round-trips.
                count_query = f"SELECT COUNT(*) AS cnt FROM ({union_cte}) AS all_rows"
                total_row = await conn.fetchrow(count_query, *params)
                total = int(total_row["cnt"]) if total_row else 0

                if total == 0 or offset >= total:
                    return total, []

                page_query = f"""
                    SELECT * FROM ({union_cte}) AS all_rows
                    ORDER BY timestamp
                    LIMIT ${idx} OFFSET ${idx + 1}
                """
                params.extend([limit, offset])
                rows = await conn.fetch(page_query, *params)

                metrics = []
                for row in rows:
                    data = dict(row)
                    if "estimation_reasons" in data and isinstance(data["estimation_reasons"], str):
                        try:
                            import json as _json

                            data["estimation_reasons"] = _json.loads(data["estimation_reasons"])
                        except Exception:
                            data["estimation_reasons"] = []
                    metrics.append(CombinedMetric(**data))

                return total, metrics
        except Exception as e:
            logger.error("Error in read_combined_metrics_page: %s", e)
            raise QueryError(f"read_combined_metrics_page failed: {e}") from e

    async def read_latest_per_pod(
        self,
        start_time: datetime,
        end_time: datetime,
    ) -> List[CombinedMetric]:
        """Return the single most-recent metric snapshot for each (namespace, pod_name).

        Uses PostgreSQL DISTINCT ON to avoid loading full history into memory.
        This replaces the pattern of reading all rows and deduplicating in Python.
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                query = """
                    SELECT DISTINCT ON (namespace, pod_name) *
                    FROM combined_metrics
                    WHERE timestamp >= $1 AND timestamp < $2
                    ORDER BY namespace, pod_name, timestamp DESC
                """
                rows = await conn.fetch(query, start_time, end_time)
                metrics = []
                for row in rows:
                    import json as _json

                    data = dict(row)
                    data.pop("id", None)
                    if "estimation_reasons" in data and isinstance(data["estimation_reasons"], str):
                        try:
                            data["estimation_reasons"] = _json.loads(data["estimation_reasons"])
                        except Exception:
                            data["estimation_reasons"] = []
                    metrics.append(CombinedMetric(**data))
                return metrics
        except Exception as e:
            logger.error("Error in read_latest_per_pod: %s", e)
            raise QueryError(f"read_latest_per_pod failed: {e}") from e

    async def recompute_carbon_with_latest_intensities(
        self,
        carbon_intensity_repo: CarbonIntensityRepository,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> int:
        """Set-based recompute of pod and container carbon figures (A9, BUG-06).

        One UPDATE per table joins each row in ``[start, end)`` to the latest
        measured carbon intensity at or before its normalized lookup time,
        provided the point is younger than ``INTENSITY_MAX_AGE``. Only rows
        whose intensity or ``intensity_source`` changes are written. Both
        UPDATEs run in one transaction so container rows never drift from
        their pod rows. See the base-class docstring for the full semantics.
        """
        from ...core.config import get_config as _get_cfg

        cfg = _get_cfg()
        granularity = getattr(cfg, "NORMALIZATION_GRANULARITY", "hour")
        if granularity in ("hour", "day"):
            lookup_expr = f"date_trunc('{granularity}', t.timestamp AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'"
        else:
            lookup_expr = "t.timestamp"
        prefix = INTENSITY_FALLBACK_REASON_PREFIX
        # Legacy flags: drop the intensity-default reason and clear is_estimated when it
        # was the only one. Nested CASEs keep the jsonb cast to rows written by json.dumps.
        has_fallback_reason = f"""t.estimation_reasons LIKE '[%"{prefix}%]'"""
        other_reasons = (
            "FROM jsonb_array_elements_text(t.estimation_reasons::jsonb) WITH ORDINALITY AS r(value, ord) "
            f"WHERE r.value NOT LIKE '{prefix}%'"
        )
        total = 0
        try:
            async with self.db_manager.connection_scope() as conn, conn.transaction():
                for table in ("combined_metrics", "container_metrics"):
                    ns_clause = "AND t.namespace = $6" if namespace else ""
                    query = f"""
                        WITH lookup AS (
                            SELECT t.id AS row_id, t.emaps_zone AS zone, {lookup_expr} AS lookup_ts
                            FROM {table} AS t
                            WHERE t.timestamp >= $2 AND t.timestamp < $3
                              AND t.emaps_zone IS NOT NULL
                              {ns_clause}
                        ),
                        fresh AS (
                            SELECT l.row_id,
                                   ci.carbon_intensity AS intensity,
                                   CASE WHEN ci.is_estimated IS FALSE THEN 'consolidated'
                                        ELSE 'realtime' END AS source
                            FROM lookup AS l
                            JOIN LATERAL (
                                SELECT c.carbon_intensity, c.is_estimated, c.datetime
                                FROM carbon_intensity_history AS c
                                WHERE c.zone = l.zone AND c.datetime <= l.lookup_ts
                                  AND {measured_intensity_sql("c")}
                                ORDER BY c.datetime DESC
                                LIMIT 1
                            ) AS ci ON TRUE
                            WHERE ci.datetime >= l.lookup_ts - $4::interval
                        ),
                        updated AS (
                            UPDATE {table} AS t
                            SET grid_intensity = f.intensity,
                                co2e_grams = (COALESCE(t.joules, 0) / $5) * COALESCE(t.pue, $1) * f.intensity,
                                intensity_source = f.source,
                                estimation_reasons = CASE WHEN {has_fallback_reason}
                                    THEN (SELECT COALESCE(jsonb_agg(r.value ORDER BY r.ord), '[]'::jsonb)::text
                                          {other_reasons})
                                    ELSE t.estimation_reasons END,
                                is_estimated = CASE WHEN {has_fallback_reason}
                                    THEN CASE WHEN EXISTS (SELECT 1 {other_reasons}) THEN t.is_estimated ELSE FALSE END
                                    ELSE t.is_estimated END
                            FROM fresh AS f
                            WHERE t.id = f.row_id
                              AND (t.grid_intensity IS DISTINCT FROM f.intensity
                                   OR t.intensity_source IS DISTINCT FROM f.source)
                            RETURNING t.id
                        )
                        SELECT count(*) FROM updated
                    """
                    params: list = [cfg.DEFAULT_PUE, start_time, end_time, cfg.intensity_max_age, cfg.JOULES_PER_KWH]
                    if namespace:
                        params.append(namespace)
                    count = await conn.fetchval(query, *params)
                    total += int(count or 0)
                if not namespace:
                    # The __idle__ rows of the same steps (NRG-09): same intensity, so pod and
                    # idle rows of a node keep adding up. Idle rows have no namespace: a
                    # namespace-filtered run leaves them to the next unfiltered one.
                    count = await conn.fetchval(
                        f"""
                        WITH lookup AS (
                            SELECT t.id AS row_id, t.emaps_zone AS zone, {lookup_expr} AS lookup_ts
                            FROM node_idle_metrics AS t
                            WHERE t.timestamp >= $2 AND t.timestamp < $3 AND t.emaps_zone IS NOT NULL
                        ),
                        fresh AS (
                            SELECT l.row_id,
                                   ci.carbon_intensity AS intensity,
                                   CASE WHEN ci.is_estimated IS FALSE THEN 'consolidated'
                                        ELSE 'realtime' END AS source
                            FROM lookup AS l
                            JOIN LATERAL (
                                SELECT c.carbon_intensity, c.is_estimated, c.datetime
                                FROM carbon_intensity_history AS c
                                WHERE c.zone = l.zone AND c.datetime <= l.lookup_ts
                                  AND {measured_intensity_sql("c")}
                                ORDER BY c.datetime DESC
                                LIMIT 1
                            ) AS ci ON TRUE
                            WHERE ci.datetime >= l.lookup_ts - $4::interval
                        ),
                        updated AS (
                            UPDATE node_idle_metrics AS t
                            SET grid_intensity = f.intensity,
                                co2e_grams = (COALESCE(t.joules, 0) / $5) * COALESCE(t.pue, $1) * f.intensity,
                                intensity_source = f.source
                            FROM fresh AS f
                            WHERE t.id = f.row_id
                              AND (t.grid_intensity IS DISTINCT FROM f.intensity
                                   OR t.intensity_source IS DISTINCT FROM f.source)
                            RETURNING t.id
                        )
                        SELECT count(*) FROM updated
                        """,
                        cfg.DEFAULT_PUE,
                        start_time,
                        end_time,
                        cfg.intensity_max_age,
                        cfg.JOULES_PER_KWH,
                    )
                    total += int(count or 0)
                return total
        except Exception as e:
            logger.error("Error recomputing combined metrics carbon: %s", e)
            raise QueryError(f"Error recomputing combined metrics carbon: {e}") from e

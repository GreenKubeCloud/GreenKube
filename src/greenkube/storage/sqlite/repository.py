import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Any, List, Mapping, Optional

import aiosqlite

from greenkube.core.observability import record_metric_provenance
from greenkube.models.metrics import CombinedMetric
from greenkube.models.provenance import INTENSITY_FALLBACK_REASON_PREFIX
from greenkube.utils.date_utils import ensure_utc, to_iso_z

from ...core.exceptions import QueryError
from ..base_repository import (
    CarbonIntensityRepository,
    CombinedMetricsRepository,
    ContainerMetricsRepository,
    IntensityPoint,
    is_persistable_intensity_record,
    measured_intensity_sql,
)

logger = logging.getLogger(__name__)


def _oldest_fresh_intensity(normalized_ts: str) -> str:
    """Lower bound of the intensity points still fresh at ``normalized_ts`` (INTENSITY_MAX_AGE)."""
    from greenkube.core.config import get_config

    try:
        reference = ensure_utc(normalized_ts)
    except ValueError:
        return ""
    return to_iso_z(reference - get_config().intensity_max_age)


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
)


def _quote_idle(column: str) -> str:
    return '"timestamp"' if column == "timestamp" else column


#: Extra container-scoped columns (A7 limits, A9 intensity zone).
CONTAINER_EXTRA_COLUMNS: tuple[str, ...] = ("emaps_zone", "cpu_limit", "memory_limit")


def _provenance_values(metric: CombinedMetric) -> tuple[Any, ...]:
    return tuple(getattr(metric, column) for column in PROVENANCE_COLUMNS)


def _provenance_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = set(row.keys())
    return {column: row[column] for column in PROVENANCE_COLUMNS if column in keys}


#: Impact Model allocation columns of raw metric rows (B2, B5, migration 0050).
ALLOCATION_COLUMNS: tuple[str, ...] = ("model_version", "gpu_power_watts")


def _allocation_values(metric: CombinedMetric) -> tuple[Any, ...]:
    return tuple(getattr(metric, column) for column in ALLOCATION_COLUMNS)


#: Where the node and pod metadata of a raw row came from (WP-01.1, migration 0120).
ROW_SOURCE_COLUMNS: tuple[str, ...] = ("node_source", "metadata_source")


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
        f"{column} = excluded.{column}"
        for column in ("owner_kind", "owner_name", "cpu_request", "memory_request", "metadata_source", "node_source")
    )
    return f"{assignments}\nWHERE NOT (COALESCE(excluded.metadata_source, '') = 'missing' AND ({has_metadata}))"


def _allocation_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = set(row.keys())
    return {column: row[column] for column in ALLOCATION_COLUMNS if column in keys}


def _row_source_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    keys = set(row.keys())
    return {column: row[column] for column in ROW_SOURCE_COLUMNS if column in keys}


def _container_metric_from_row(row: Mapping[str, Any], *, hourly: bool = False) -> CombinedMetric:
    data = dict(row)
    data.pop("id", None)
    if hourly:
        # sample_count is kept: hourly rows stand for several raw samples (BUG-29).
        data["timestamp"] = data.pop("hour_bucket", None)
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


class SQLiteCarbonIntensityRepository(CarbonIntensityRepository):
    """
    SQLite implementation for carbon intensity data storage.
    Handles saving and retrieving carbon intensity history records.
    """

    def __init__(self, db_manager):
        """
        Initializes the repository with a database manager.

        Args:
            db_manager: The DatabaseManager instance.
        """
        self.db_manager = db_manager

    async def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
        """
        Retrieves the latest carbon intensity for a given zone at or before a specific timestamp.
        Consistent with the PostgreSQL implementation (no arbitrary lookback window).
        """
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row

                # Normalize the query timestamp to ensure it matches the stored format (Z suffix)
                try:
                    dt = ensure_utc(timestamp)
                    normalized_ts = to_iso_z(dt)
                except ValueError:
                    normalized_ts = timestamp

                query = f"""
                    SELECT carbon_intensity
                    FROM carbon_intensity_history
                    WHERE zone = ? AND datetime <= ? AND datetime >= ?
                      AND {measured_intensity_sql()}
                    ORDER BY datetime DESC
                    LIMIT 1
                """
                params = (zone, normalized_ts, _oldest_fresh_intensity(normalized_ts))

                async with conn.execute(query, params) as cursor:
                    result = await cursor.fetchone()
                    return result["carbon_intensity"] if result else None
        except sqlite3.Error as e:
            logger.error("Database error in get_for_zone_at_time for zone %s at %s: %s", zone, timestamp, e)
            raise QueryError(f"Database error in get_for_zone_at_time: {e}") from e
        except Exception as e:
            logger.error("Unexpected error in get_for_zone_at_time: %s", e)
            raise QueryError(f"Unexpected error in get_for_zone_at_time: {e}") from e

    async def get_intensity_point(self, zone: str, timestamp: str) -> Optional[IntensityPoint]:
        """Return the latest stored point at or before ``timestamp`` with its time and flags."""
        try:
            normalized_ts = to_iso_z(ensure_utc(timestamp))
        except ValueError:
            normalized_ts = timestamp
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    f"""
                    SELECT carbon_intensity, datetime, is_estimated, estimation_method
                    FROM carbon_intensity_history
                    WHERE zone = ? AND datetime <= ? AND {measured_intensity_sql()}
                    ORDER BY datetime DESC
                    LIMIT 1
                    """,
                    (zone, normalized_ts),
                ) as cursor:
                    row = await cursor.fetchone()
        except sqlite3.Error as e:
            raise QueryError(f"Database error in get_intensity_point: {e}") from e
        if row is None or row["carbon_intensity"] is None:
            return None
        return IntensityPoint(
            value=float(row["carbon_intensity"]),
            observed_at=ensure_utc(row["datetime"]) if row["datetime"] else None,
            is_estimated=None if row["is_estimated"] is None else bool(row["is_estimated"]),
            estimation_method=row["estimation_method"],
        )

    async def save_history(self, history_data: list, zone: str) -> int:
        """
        Saves historical carbon intensity data to the SQLite database.
        It ignores records that would be duplicates based on zone and datetime.
        Static zone defaults are never persisted: they are computed at read time.
        """
        saved_count = 0

        try:
            async with self.db_manager.connection_scope() as conn:
                for record in history_data:
                    # Basic validation that record is a dictionary
                    if not isinstance(record, dict):
                        logging.warning("Skipping invalid record (not a dict): %s", record)
                        continue
                    if not is_persistable_intensity_record(record):
                        logger.debug("Skipping non-measured intensity record for zone %s: %s", zone, record)
                        continue

                    try:
                        # Normalize datetime
                        raw_dt = record.get("datetime")
                        if raw_dt:
                            normalized_dt = to_iso_z(ensure_utc(raw_dt))
                        else:
                            normalized_dt = None

                        # Use default value None if key is missing
                        cursor = await conn.execute(
                            """
                            INSERT INTO carbon_intensity_history
                                (zone, carbon_intensity, datetime, updated_at, created_at,
                                 emission_factor_type, is_estimated, estimation_method)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(zone, datetime)
                            DO UPDATE SET
                                carbon_intensity = excluded.carbon_intensity,
                                updated_at = excluded.updated_at,
                                is_estimated = excluded.is_estimated,
                                estimation_method = excluded.estimation_method,
                                emission_factor_type = excluded.emission_factor_type;
                        """,
                            (
                                zone,
                                record.get("carbonIntensity"),
                                normalized_dt,
                                record.get("updatedAt"),
                                record.get("createdAt"),
                                record.get("emissionFactorType"),
                                record.get("isEstimated"),
                                record.get("estimationMethod"),
                            ),
                        )
                        # cursor.rowcount will be 1 for a successful insert, 0 for conflict/no insert
                        # actually for ON CONFLICT DO UPDATE it might be 1?
                        saved_count += cursor.rowcount
                    except sqlite3.Error as e:
                        # Use logging for errors
                        logging.error("Could not save record for zone %s at %s: %s", zone, record.get("datetime"), e)
                    except Exception as e:
                        # Catch potential errors from record.get() if record structure is unexpected
                        logging.error("Unexpected error processing record %s: %s", record, e)

                await conn.commit()
                return saved_count
        except sqlite3.Error as e:
            logging.error("Failed to commit transaction: %s", e)
            raise QueryError(f"Failed to commit transaction: {e}") from e
        except Exception as e:
            logging.error("Unexpected error in save_history: %s", e)
            raise QueryError(f"Unexpected error in save_history: {e}") from e


class SQLiteCombinedMetricsRepository(CombinedMetricsRepository, ContainerMetricsRepository):
    """
    SQLite implementation for combined metrics data storage.
    Handles writing and reading CombinedMetric records.
    """

    def __init__(self, db_manager):
        """
        Initializes the repository with a database manager.

        Args:
            db_manager: The DatabaseManager instance.
        """
        self.db_manager = db_manager

    async def write_combined_metrics(self, metrics: List[CombinedMetric], *, replace_containers: bool = False) -> int:
        """See :meth:`CombinedMetricsRepository.write_combined_metrics`."""
        container_metrics = [metric for metric in metrics if metric.container_name]
        metrics = [metric for metric in metrics if not metric.container_name]
        if not metrics:
            return await self.write_container_metrics(container_metrics) if container_metrics else 0

        saved_count = 0
        try:
            async with self.db_manager.connection_scope() as conn:
                for metric in metrics:
                    try:
                        timestamp_iso = to_iso_z(metric.timestamp) if metric.timestamp else None
                        grid_intensity_timestamp_iso = (
                            to_iso_z(metric.grid_intensity_timestamp) if metric.grid_intensity_timestamp else None
                        )

                        cursor = await conn.execute(
                            """
                            INSERT INTO combined_metrics
                                 (pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity, joules,
                                  cpu_request, memory_request, cpu_usage_millicores, memory_usage_bytes,
                                  network_receive_bytes, network_transmit_bytes,
                                  disk_read_bytes, disk_write_bytes,
                                  storage_request_bytes, storage_usage_bytes,
                                  ephemeral_storage_request_bytes, ephemeral_storage_usage_bytes,
                                  gpu_usage_millicores, restart_count,
                                  owner_kind, owner_name,
                                  period, "timestamp", duration_seconds,
                                  grid_intensity_timestamp, node, node_instance_type, node_zone, emaps_zone,
                                  is_estimated, estimation_reasons, embodied_co2e_grams,
                                  calculation_version,
                                  cost_cpu, cost_ram, cost_storage, cost_network, cost_lb,
                                  cost_source, usage_source, power_source, intensity_source,
                                  model_version, gpu_power_watts, node_source, metadata_source)
                            VALUES (
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?,
                                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                ?, ?, ?, ?
                            )
                            ON CONFLICT(pod_name, namespace, "timestamp") DO UPDATE SET
                                total_cost = excluded.total_cost,
                                co2e_grams = excluded.co2e_grams,
                                pue = excluded.pue,
                                grid_intensity = excluded.grid_intensity,
                                joules = excluded.joules,
                                cpu_usage_millicores = excluded.cpu_usage_millicores,
                                memory_usage_bytes = excluded.memory_usage_bytes,
                                network_receive_bytes = excluded.network_receive_bytes,
                                network_transmit_bytes = excluded.network_transmit_bytes,
                                disk_read_bytes = excluded.disk_read_bytes,
                                disk_write_bytes = excluded.disk_write_bytes,
                                storage_request_bytes = excluded.storage_request_bytes,
                                storage_usage_bytes = excluded.storage_usage_bytes,
                                ephemeral_storage_request_bytes = excluded.ephemeral_storage_request_bytes,
                                ephemeral_storage_usage_bytes = excluded.ephemeral_storage_usage_bytes,
                                gpu_usage_millicores = excluded.gpu_usage_millicores,
                                restart_count = excluded.restart_count,
                                period = excluded.period,
                                duration_seconds = excluded.duration_seconds,
                                grid_intensity_timestamp = excluded.grid_intensity_timestamp,
                                node = excluded.node,
                                node_instance_type = excluded.node_instance_type,
                                node_zone = excluded.node_zone,
                                emaps_zone = excluded.emaps_zone,
                                is_estimated = excluded.is_estimated,
                                estimation_reasons = excluded.estimation_reasons,
                                embodied_co2e_grams = excluded.embodied_co2e_grams,
                                calculation_version = excluded.calculation_version,
                                cost_cpu = excluded.cost_cpu,
                                cost_ram = excluded.cost_ram,
                                cost_storage = excluded.cost_storage,
                                cost_network = excluded.cost_network,
                                cost_lb = excluded.cost_lb,
                                cost_source = excluded.cost_source,
                                usage_source = excluded.usage_source,
                                power_source = excluded.power_source,
                                intensity_source = excluded.intensity_source,
                                model_version = excluded.model_version,
                                gpu_power_watts = excluded.gpu_power_watts,
                        """
                            + _metadata_upsert("combined_metrics")
                            + ";",
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
                                timestamp_iso,
                                metric.duration_seconds,
                                grid_intensity_timestamp_iso,
                                metric.node,
                                metric.node_instance_type,
                                metric.node_zone,
                                metric.emaps_zone,
                                metric.is_estimated,
                                json.dumps(metric.estimation_reasons) if metric.estimation_reasons else "[]",
                                metric.embodied_co2e_grams,
                                metric.calculation_version,
                                *_provenance_values(metric),
                                *_allocation_values(metric),
                                *_row_source_values(metric),
                            ),
                        )
                        saved_count += cursor.rowcount
                    except sqlite3.Error as e:
                        logging.error("Could not save combined metric for pod %s: %s", metric.pod_name, e)
                    except Exception as e:
                        logging.error("Unexpected error processing combined metric %s: %s", metric.pod_name, e)

                # Container rows are rewritten with their pod rows, in the same
                # transaction, so pod and container rows of a step agree (review 4).
                if replace_containers:
                    await self._delete_stale_container_rows(conn, metrics, container_metrics)
                saved_count += await self._upsert_container_rows(conn, container_metrics)
                await conn.commit()
        except sqlite3.Error as e:
            logging.error("Failed to commit transaction for combined_metrics: %s", e)
            raise QueryError(f"Failed to commit combined_metrics: {e}") from e
        except Exception as e:
            logging.error("Unexpected error in write_combined_metrics: %s", e)
            raise QueryError(f"Unexpected error in write_combined_metrics: {e}") from e
        record_metric_provenance(metrics)
        if container_metrics:
            record_metric_provenance(container_metrics)
        return saved_count

    @staticmethod
    async def _delete_stale_container_rows(
        conn, pod_rows: List[CombinedMetric], container_rows: List[CombinedMetric]
    ) -> None:
        """Delete container rows of re-written pod rows that the new batch no longer produces.

        Pod rows without metadata (``missing``) are skipped: the stored pod row
        and its containers are kept when they had metadata (review 4).
        """
        new_containers: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        for row in container_rows:
            if row.timestamp is not None and row.container_name:
                new_containers[(row.namespace, row.pod_name, to_iso_z(row.timestamp))].add(row.container_name)
        for row in pod_rows:
            if row.timestamp is None or row.metadata_source == "missing":
                continue
            key = (row.namespace, row.pod_name, to_iso_z(row.timestamp))
            keep = sorted(new_containers.get(key, ()))
            placeholders = ", ".join("?" for _ in keep)
            await conn.execute(
                "DELETE FROM container_metrics WHERE namespace = ? AND pod_name = ? AND timestamp = ?"
                + (f" AND container_name NOT IN ({placeholders})" if keep else ""),
                (*key, *keep),
            )

    async def read_combined_metrics(self, start_time: datetime, end_time: datetime) -> List[CombinedMetric]:
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    """
                    SELECT pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity, joules,
                           cpu_request, memory_request, cpu_usage_millicores, memory_usage_bytes,
                           network_receive_bytes, network_transmit_bytes,
                           disk_read_bytes, disk_write_bytes,
                           storage_request_bytes, storage_usage_bytes,
                           ephemeral_storage_request_bytes, ephemeral_storage_usage_bytes,
                           gpu_usage_millicores, restart_count,
                           owner_kind, owner_name,
                           period, "timestamp", duration_seconds,
                           grid_intensity_timestamp, node, node_instance_type, node_zone, emaps_zone,
                           is_estimated, estimation_reasons, embodied_co2e_grams,
                           calculation_version,
                           cost_cpu, cost_ram, cost_storage, cost_network, cost_lb,
                           cost_source, usage_source, power_source, intensity_source,
                           model_version, gpu_power_watts, node_source, metadata_source
                    FROM combined_metrics
                    WHERE "timestamp" >= ? AND "timestamp" < ?
                """,
                    (to_iso_z(start_time), to_iso_z(end_time)),
                ) as cursor:
                    rows = await cursor.fetchall()
                    metrics = []
                    for row in rows:
                        estimation_reasons = []
                        if row["estimation_reasons"]:
                            try:
                                estimation_reasons = json.loads(row["estimation_reasons"])
                            except json.JSONDecodeError:
                                pass

                        metrics.append(
                            CombinedMetric(
                                pod_name=row["pod_name"],
                                namespace=row["namespace"],
                                total_cost=row["total_cost"],
                                co2e_grams=row["co2e_grams"],
                                pue=row["pue"],
                                grid_intensity=row["grid_intensity"],
                                joules=row["joules"],
                                cpu_request=row["cpu_request"],
                                memory_request=row["memory_request"],
                                cpu_usage_millicores=row["cpu_usage_millicores"],
                                memory_usage_bytes=row["memory_usage_bytes"],
                                network_receive_bytes=row["network_receive_bytes"],
                                network_transmit_bytes=row["network_transmit_bytes"],
                                disk_read_bytes=row["disk_read_bytes"],
                                disk_write_bytes=row["disk_write_bytes"],
                                storage_request_bytes=row["storage_request_bytes"],
                                storage_usage_bytes=row["storage_usage_bytes"],
                                ephemeral_storage_request_bytes=row["ephemeral_storage_request_bytes"],
                                ephemeral_storage_usage_bytes=row["ephemeral_storage_usage_bytes"],
                                gpu_usage_millicores=row["gpu_usage_millicores"],
                                restart_count=row["restart_count"],
                                owner_kind=row["owner_kind"],
                                owner_name=row["owner_name"],
                                period=row["period"],
                                timestamp=datetime.fromisoformat(row["timestamp"]) if row["timestamp"] else None,
                                duration_seconds=row["duration_seconds"],
                                grid_intensity_timestamp=datetime.fromisoformat(row["grid_intensity_timestamp"])
                                if row["grid_intensity_timestamp"]
                                else None,
                                node=row["node"],
                                node_instance_type=row["node_instance_type"],
                                node_zone=row["node_zone"],
                                emaps_zone=row["emaps_zone"],
                                is_estimated=bool(row["is_estimated"]),
                                estimation_reasons=estimation_reasons,
                                embodied_co2e_grams=row["embodied_co2e_grams"],
                                calculation_version=row["calculation_version"],
                                **_provenance_from_row(row),
                                **_allocation_from_row(row),
                                **_row_source_from_row(row),
                            )
                        )
                    return metrics
        except sqlite3.Error as e:
            logging.error("Could not read combined metrics: %s", e)
            raise QueryError(f"Could not read combined metrics: {e}") from e
        except Exception as e:
            logging.error("Unexpected error reading combined metrics: %s", e)
            raise QueryError(f"Unexpected error reading combined metrics: {e}") from e

    async def count_combined_metrics_smart(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> Optional[int]:
        """Count raw and hourly combined rows without materializing them."""
        from datetime import timedelta, timezone

        from greenkube.core.config import get_config

        cutoff = datetime.now(timezone.utc) - timedelta(hours=get_config().METRICS_COMPRESSION_AGE_HOURS)
        start_aware = ensure_utc(start_time)
        end_aware = ensure_utc(end_time)
        try:
            async with self.db_manager.connection_scope() as conn:
                total = 0
                if end_aware >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_aware, cutoff - timedelta(minutes=1))
                    query = 'SELECT COUNT(*) FROM combined_metrics WHERE "timestamp" >= ? AND "timestamp" < ?'
                    params: list[Any] = [to_iso_z(raw_start), to_iso_z(end_aware)]
                    if namespace:
                        query += " AND namespace = ?"
                        params.append(namespace)
                    async with conn.execute(query, params) as cursor:
                        row = await cursor.fetchone()
                        total += int(row[0] or 0)
                if start_aware < cutoff:
                    hourly_end = min(end_aware, cutoff)
                    query = "SELECT COUNT(*) FROM combined_metrics_hourly WHERE hour_bucket >= ? AND hour_bucket < ?"
                    params = [to_iso_z(start_aware), to_iso_z(hourly_end)]
                    if namespace:
                        query += " AND namespace = ?"
                        params.append(namespace)
                    async with conn.execute(query, params) as cursor:
                        row = await cursor.fetchone()
                        total += int(row[0] or 0)
                return total
        except sqlite3.Error as exc:
            logger.warning("Could not count combined metrics: %s", exc)
            return None

    async def count_container_metrics_smart(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> Optional[int]:
        """Count raw and hourly container rows without materializing them."""
        from datetime import timedelta, timezone

        from greenkube.core.config import get_config

        cutoff = datetime.now(timezone.utc) - timedelta(hours=get_config().METRICS_COMPRESSION_AGE_HOURS)
        start_aware = ensure_utc(start_time)
        end_aware = ensure_utc(end_time)
        try:
            async with self.db_manager.connection_scope() as conn:
                total = 0
                if end_aware >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_aware, cutoff - timedelta(minutes=1))
                    query = "SELECT COUNT(*) FROM container_metrics WHERE timestamp >= ? AND timestamp < ?"
                    params: list[Any] = [to_iso_z(raw_start), to_iso_z(end_aware)]
                    if namespace:
                        query += " AND namespace = ?"
                        params.append(namespace)
                    async with conn.execute(query, params) as cursor:
                        row = await cursor.fetchone()
                        total += int(row[0] or 0)
                if start_aware < cutoff:
                    hourly_end = min(end_aware, cutoff)
                    query = "SELECT COUNT(*) FROM container_metrics_hourly WHERE hour_bucket >= ? AND hour_bucket < ?"
                    params = [to_iso_z(start_aware), to_iso_z(hourly_end)]
                    if namespace:
                        query += " AND namespace = ?"
                        params.append(namespace)
                    async with conn.execute(query, params) as cursor:
                        row = await cursor.fetchone()
                        total += int(row[0] or 0)
                return total
        except sqlite3.Error as exc:
            logger.warning("Could not count container metrics: %s", exc)
            return None

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
                conn.row_factory = aiosqlite.Row
                filters = ['"timestamp" >= ?', '"timestamp" < ?']
                params: list = [to_iso_z(start_time), to_iso_z(end_time)]
                if namespace:
                    filters.append("namespace = ?")
                    params.append(namespace)
                count_filters = list(filters)
                count_params = list(params)
                if cursor:
                    filters.append(
                        '("timestamp" > ? OR ("timestamp" = ? AND (namespace > ? OR (namespace = ? AND pod_name > ?))))'
                    )
                    params.extend(
                        [
                            cursor["timestamp"],
                            cursor["timestamp"],
                            cursor["namespace"],
                            cursor["namespace"],
                            cursor["pod_name"],
                        ]
                    )
                where = " AND ".join(filters)
                async with conn.execute(
                    f"SELECT COUNT(*) FROM combined_metrics WHERE {' AND '.join(count_filters)}", count_params
                ) as count_cursor:
                    total = (await count_cursor.fetchone())[0]
                query = f"""
                    SELECT pod_name, namespace, total_cost, co2e_grams, pue, grid_intensity, joules,
                           cpu_request, memory_request, cpu_usage_millicores, memory_usage_bytes,
                           network_receive_bytes, network_transmit_bytes, disk_read_bytes, disk_write_bytes,
                           storage_request_bytes, storage_usage_bytes, ephemeral_storage_request_bytes,
                           ephemeral_storage_usage_bytes, gpu_usage_millicores, restart_count,
                           owner_kind, owner_name, period, "timestamp", duration_seconds,
                           grid_intensity_timestamp, node, node_instance_type, node_zone, emaps_zone,
                           is_estimated, estimation_reasons, embodied_co2e_grams, calculation_version,
                           cost_cpu, cost_ram, cost_storage, cost_network, cost_lb,
                           cost_source, usage_source, power_source, intensity_source,
                           model_version, gpu_power_watts, node_source, metadata_source
                    FROM combined_metrics WHERE {where}
                    ORDER BY "timestamp", namespace, pod_name
                    LIMIT ?
                """
                async with conn.execute(query, [*params, limit + 1]) as rows_cursor:
                    rows = await rows_cursor.fetchall()
                has_more = len(rows) > limit
                metrics = []
                for row in rows[:limit]:
                    data = dict(row)
                    for field in ("timestamp", "grid_intensity_timestamp"):
                        if data[field]:
                            data[field] = datetime.fromisoformat(data[field])
                    if data["estimation_reasons"]:
                        data["estimation_reasons"] = json.loads(data["estimation_reasons"])
                    metrics.append(CombinedMetric(**data))
                if has_more and metrics:
                    last = metrics[-1]
                    assert last.timestamp is not None
                    return (
                        total,
                        metrics,
                        {
                            "timestamp": to_iso_z(last.timestamp),
                            "namespace": last.namespace,
                            "pod_name": last.pod_name,
                        },
                    )
                return total, metrics, None
        except (sqlite3.Error, ValueError, KeyError, TypeError) as e:
            logger.error("Could not read cursor-paginated combined metrics: %s", e)
            raise QueryError(f"Could not read cursor-paginated combined metrics: {e}") from e

    async def aggregate_summary(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> dict:
        """
        Aggregates summary metrics from both raw and hourly SQLite tables.
        """
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)

        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                parts: list[str] = []
                params: list = []

                # Raw table — recent data
                if end_time >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_time, cutoff - timedelta(minutes=1))
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(f"""
                        SELECT co2e_grams, embodied_co2e_grams, total_cost, joules,
                               pod_name, namespace
                        FROM combined_metrics
                        WHERE "timestamp" >= ? AND "timestamp" < ?{ns_clause}
                    """)
                    params.extend([to_iso_z(raw_start), to_iso_z(end_time)])
                    if namespace:
                        params.append(namespace)

                # Hourly table — old data
                if start_time < cutoff:
                    hourly_end = min(end_time, cutoff)
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(f"""
                        SELECT co2e_grams, embodied_co2e_grams, total_cost, joules,
                               pod_name, namespace
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ? AND hour_bucket < ?{ns_clause}
                    """)
                    params.extend([to_iso_z(start_time), to_iso_z(hourly_end)])
                    if namespace:
                        params.append(namespace)

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
                    FROM ({union_query})
                """
                async with conn.execute(query, params) as cursor:
                    row = await cursor.fetchone()
                    return {
                        "total_co2e_grams": row["total_co2e"] or 0.0,
                        "total_embodied_co2e_grams": row["total_embodied"] or 0.0,
                        "total_cost": row["total_cost"] or 0.0,
                        "total_energy_joules": row["total_energy"] or 0.0,
                        "pod_count": row["pod_count"] or 0,
                        "namespace_count": row["namespace_count"] or 0,
                        "row_count": row["row_count"] or 0,
                    }
        except sqlite3.Error as e:
            logging.error("aggregate_summary failed: %s", e)
            raise QueryError(f"aggregate_summary failed: {e}") from e

    async def aggregate_grouped_row_count(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
        granularity: Optional[str] = None,
        group_by: str = "pod",
    ) -> int:
        """SQL COUNT of distinct groups — no CombinedMetric rows loaded into memory."""
        import sqlite3
        from datetime import timedelta
        from datetime import timezone as tz

        import aiosqlite

        from greenkube.core.config import get_config

        _SQLITE_GRAN_FMTS = {
            "hourly": "%Y-%m-%dT%H:00:00Z",
            "daily": "%Y-%m-%dT00:00:00Z",
            "monthly": "%Y-%m-01T00:00:00Z",
            "yearly": "%Y-01-01T00:00:00Z",
        }

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)
        start_aware = start_time if start_time.tzinfo else start_time.replace(tzinfo=tz.utc)
        end_aware = end_time if end_time.tzinfo else end_time.replace(tzinfo=tz.utc)

        if granularity == "weekly":
            # Floor to Monday of the ISO week (same bucket as the other backends).
            raw_ts_expr = "strftime('%Y-%m-%dT00:00:00Z', \"timestamp\", 'weekday 0', '-6 days')"
            hourly_ts_expr = "strftime('%Y-%m-%dT00:00:00Z', hour_bucket, 'weekday 0', '-6 days')"
        elif granularity in _SQLITE_GRAN_FMTS:
            ts_fmt = _SQLITE_GRAN_FMTS[granularity]
            raw_ts_expr = f"strftime('{ts_fmt}', \"timestamp\")"
            hourly_ts_expr = f"strftime('{ts_fmt}', hour_bucket)"
        else:
            raw_ts_expr = "NULL"
            hourly_ts_expr = "NULL"

        if group_by == "namespace":
            group_cols = "namespace"
        else:
            group_cols = "namespace, pod_name"

        try:
            async with self.db_manager.connection_scope() as conn:
                parts: list[str] = []
                params: list = []

                if end_aware >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_aware, cutoff - timedelta(minutes=1))
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(
                        f"SELECT {group_cols}, {raw_ts_expr} AS ts_bucket "
                        f'FROM combined_metrics WHERE "timestamp" >= ? AND "timestamp" < ?{ns_clause}'
                    )
                    params.extend([to_iso_z(raw_start), to_iso_z(end_time)])
                    if namespace:
                        params.append(namespace)

                if start_aware < cutoff:
                    hourly_end = min(end_aware, cutoff)
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(
                        f"SELECT {group_cols}, {hourly_ts_expr} AS ts_bucket "
                        f"FROM combined_metrics_hourly WHERE hour_bucket >= ? AND hour_bucket < ?{ns_clause}"
                    )
                    params.extend([to_iso_z(start_time), to_iso_z(hourly_end)])
                    if namespace:
                        params.append(namespace)

                if not parts:
                    return 0

                union_cte = " UNION ALL ".join(parts)
                if group_by == "namespace":
                    distinct_cols = "namespace, ts_bucket"
                else:
                    distinct_cols = "namespace, pod_name, ts_bucket"

                query = (
                    f"SELECT COUNT(*) AS group_count FROM "
                    f"(SELECT DISTINCT {distinct_cols} FROM ({union_cte}) AS all_rows) AS groups"
                )
                conn.row_factory = aiosqlite.Row
                async with conn.execute(query, params) as cursor:
                    row = await cursor.fetchone()
                    return int(row["group_count"]) if row else 0
        except sqlite3.Error as e:
            logging.error("aggregate_grouped_row_count failed: %s", e)
            raise QueryError(f"aggregate_grouped_row_count failed: {e}") from e

    async def aggregate_timeseries(
        self,
        start_time: datetime,
        end_time: datetime,
        granularity: str = "hour",
        namespace: Optional[str] = None,
    ) -> List[dict]:
        """
        Groups time-series data by granularity from both raw and hourly SQLite tables.
        """
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        _SQLITE_FORMATS = {
            "hour": "%Y-%m-%dT%H:00:00Z",
            "day": "%Y-%m-%dT00:00:00Z",
            "week": None,  # handled separately: floor to Monday via date modifier
            "month": "%Y-%m-01T00:00:00Z",
        }
        ts_format = _SQLITE_FORMATS.get(granularity, "%Y-%m-%dT%H:00:00Z")

        # For weekly buckets SQLite has no date_trunc; compute Monday of each week.
        if granularity == "week":
            ts_bucket_expr = "strftime('%Y-%m-%dT00:00:00Z', ts, 'weekday 0', '-6 days')"
        else:
            ts_bucket_expr = f"strftime('{ts_format}', ts)"

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)

        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                parts: list[str] = []
                params: list = []

                # Raw table — recent data
                if end_time >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_time, cutoff - timedelta(minutes=1))
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(f"""
                        SELECT "timestamp" AS ts, co2e_grams, embodied_co2e_grams,
                               total_cost, joules, cpu_usage_millicores, memory_usage_bytes
                        FROM combined_metrics
                        WHERE "timestamp" >= ? AND "timestamp" < ?{ns_clause}
                    """)
                    params.extend([to_iso_z(raw_start), to_iso_z(end_time)])
                    if namespace:
                        params.append(namespace)

                # Hourly table — old data
                if start_time < cutoff:
                    hourly_end = min(end_time, cutoff)
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(f"""
                        SELECT hour_bucket AS ts, co2e_grams, embodied_co2e_grams,
                               total_cost, joules, cpu_usage_avg AS cpu_usage_millicores,
                               memory_usage_avg AS memory_usage_bytes
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ? AND hour_bucket < ?{ns_clause}
                    """)
                    params.extend([to_iso_z(start_time), to_iso_z(hourly_end)])
                    if namespace:
                        params.append(namespace)

                if not parts:
                    return []

                union_query = " UNION ALL ".join(parts)
                query = f"""
                    SELECT
                        {ts_bucket_expr} AS ts_bucket,
                        COALESCE(SUM(co2e_grams), 0)          AS co2e_grams,
                        COALESCE(SUM(embodied_co2e_grams), 0) AS embodied_co2e_grams,
                        COALESCE(SUM(total_cost), 0)          AS total_cost,
                        COALESCE(SUM(joules), 0)              AS energy_joules,
                        SUM(cpu_usage_millicores) AS cpu_usage_millicores,
                        SUM(memory_usage_bytes) AS memory_usage_bytes
                    FROM ({union_query})
                    GROUP BY ts_bucket
                    ORDER BY ts_bucket
                """
                async with conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [
                        {
                            "timestamp": row["ts_bucket"],
                            "co2e_grams": row["co2e_grams"],
                            "embodied_co2e_grams": row["embodied_co2e_grams"],
                            "total_cost": row["total_cost"],
                            "energy_joules": row["energy_joules"],
                            "cpu_usage_millicores": row["cpu_usage_millicores"],
                            "memory_usage_bytes": row["memory_usage_bytes"],
                        }
                        for row in rows
                    ]
        except sqlite3.Error as e:
            logging.error("aggregate_timeseries failed: %s", e)
            raise QueryError(f"aggregate_timeseries failed: {e}") from e

    async def read_hourly_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[CombinedMetric]:
        """Read pre-aggregated hourly metrics from the hourly table."""
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                if namespace:
                    query = """
                        SELECT * FROM combined_metrics_hourly
                        WHERE hour_bucket >= ? AND hour_bucket < ?
                          AND namespace = ?
                        ORDER BY hour_bucket
                    """
                    params = (to_iso_z(start_time), to_iso_z(end_time), namespace)
                else:
                    query = """
                        SELECT * FROM combined_metrics_hourly
                        WHERE hour_bucket >= ? AND hour_bucket < ?
                        ORDER BY hour_bucket
                    """
                    params = (to_iso_z(start_time), to_iso_z(end_time))

                async with conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    metrics = []
                    for row in rows:
                        estimation_reasons = []
                        if row["estimation_reasons"]:
                            try:
                                estimation_reasons = json.loads(row["estimation_reasons"])
                            except json.JSONDecodeError:
                                pass
                        metrics.append(
                            CombinedMetric(
                                pod_name=row["pod_name"],
                                namespace=row["namespace"],
                                total_cost=row["total_cost"],
                                co2e_grams=row["co2e_grams"],
                                embodied_co2e_grams=row["embodied_co2e_grams"],
                                pue=row["pue"],
                                grid_intensity=row["grid_intensity"],
                                joules=row["joules"],
                                cpu_request=row["cpu_request"],
                                memory_request=row["memory_request"],
                                sample_count=row["sample_count"],
                                cpu_usage_millicores=row["cpu_usage_avg"],
                                cpu_usage_max_millicores=row["cpu_usage_max"],
                                memory_usage_bytes=row["memory_usage_avg"],
                                memory_usage_max_bytes=row["memory_usage_max"],
                                network_receive_bytes=row["network_receive_bytes"],
                                network_transmit_bytes=row["network_transmit_bytes"],
                                disk_read_bytes=row["disk_read_bytes"],
                                disk_write_bytes=row["disk_write_bytes"],
                                storage_request_bytes=row["storage_request_bytes"],
                                storage_usage_bytes=row["storage_usage_bytes"],
                                gpu_usage_millicores=row["gpu_usage_millicores"],
                                restart_count=row["restart_count"],
                                owner_kind=row["owner_kind"],
                                owner_name=row["owner_name"],
                                timestamp=datetime.fromisoformat(row["hour_bucket"]) if row["hour_bucket"] else None,
                                duration_seconds=row["duration_seconds"],
                                node=row["node"],
                                node_instance_type=row["node_instance_type"],
                                node_zone=row["node_zone"],
                                emaps_zone=row["emaps_zone"],
                                is_estimated=bool(row["is_estimated"]),
                                estimation_reasons=estimation_reasons,
                                calculation_version=row["calculation_version"],
                                **_provenance_from_row(row),
                                **_allocation_from_row(row),
                            )
                        )
                    return metrics
        except sqlite3.Error as e:
            logging.error("Could not read hourly metrics: %s", e)
            raise QueryError(f"Could not read hourly metrics: {e}") from e

    async def write_container_metrics(self, metrics: List[CombinedMetric]) -> int:
        if not metrics:
            return 0
        try:
            async with self.db_manager.connection_scope() as conn:
                saved_count = await self._upsert_container_rows(conn, metrics)
                await conn.commit()
            record_metric_provenance(metrics)
            return saved_count
        except sqlite3.Error as e:
            logging.error("Could not write container metrics: %s", e)
            raise QueryError(f"Could not write container metrics: {e}") from e

    @staticmethod
    async def _upsert_container_rows(conn, metrics: List[CombinedMetric]) -> int:
        """Upsert container rows on ``conn`` without committing."""
        saved_count = 0
        for metric in metrics:
            container_name = metric.container_name
            timestamp = metric.timestamp
            if not container_name or timestamp is None:
                raise ValueError("container metrics require a container name and timestamp")
            cursor = await conn.execute(
                """
                INSERT INTO container_metrics (
                    pod_name, namespace, container_name, total_cost, co2e_grams,
                    embodied_co2e_grams, pue, grid_intensity, joules,
                    cpu_request, memory_request, cpu_usage_millicores, cpu_usage_max_millicores,
                    memory_usage_bytes, memory_usage_max_bytes, owner_kind, owner_name,
                    timestamp, duration_seconds, is_estimated, estimation_reasons, calculation_version,
                    cost_cpu, cost_ram, cost_storage, cost_network, cost_lb,
                    cost_source, usage_source, power_source, intensity_source,
                    emaps_zone, cpu_limit, memory_limit, model_version, gpu_power_watts,
                    node_source, metadata_source
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(namespace, pod_name, container_name, timestamp) DO UPDATE SET
                    total_cost = excluded.total_cost,
                    co2e_grams = excluded.co2e_grams,
                    embodied_co2e_grams = excluded.embodied_co2e_grams,
                    pue = excluded.pue,
                    grid_intensity = excluded.grid_intensity,
                    joules = excluded.joules,
                    cpu_request = excluded.cpu_request,
                    memory_request = excluded.memory_request,
                    cpu_usage_millicores = excluded.cpu_usage_millicores,
                    cpu_usage_max_millicores = excluded.cpu_usage_max_millicores,
                    memory_usage_bytes = excluded.memory_usage_bytes,
                    memory_usage_max_bytes = excluded.memory_usage_max_bytes,
                    owner_kind = excluded.owner_kind,
                    owner_name = excluded.owner_name,
                    duration_seconds = excluded.duration_seconds,
                    is_estimated = excluded.is_estimated,
                    estimation_reasons = excluded.estimation_reasons,
                    calculation_version = excluded.calculation_version,
                    cost_cpu = excluded.cost_cpu,
                    cost_ram = excluded.cost_ram,
                    cost_storage = excluded.cost_storage,
                    cost_network = excluded.cost_network,
                    cost_lb = excluded.cost_lb,
                    cost_source = excluded.cost_source,
                    usage_source = excluded.usage_source,
                    power_source = excluded.power_source,
                    intensity_source = excluded.intensity_source,
                    emaps_zone = excluded.emaps_zone,
                    cpu_limit = excluded.cpu_limit,
                    memory_limit = excluded.memory_limit,
                    model_version = excluded.model_version,
                    gpu_power_watts = excluded.gpu_power_watts,
                    node_source = excluded.node_source,
                    metadata_source = excluded.metadata_source
                """,
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
                    to_iso_z(timestamp),
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
                ),
            )
            saved_count += cursor.rowcount
        return saved_count

    async def write_idle_metrics(self, metrics: List[Any]) -> int:
        """Upserts ``__idle__`` rows (one per node and step) into ``node_idle_metrics`` (B2)."""
        if not metrics:
            return 0
        columns = IDLE_METRIC_COLUMNS
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(
            f"{column} = excluded.{column}" for column in columns if column not in ("node", "timestamp")
        )
        query = (
            f"INSERT INTO node_idle_metrics ({', '.join(_quote_idle(c) for c in columns)}) VALUES ({placeholders}) "
            f'ON CONFLICT(node, "timestamp") DO UPDATE SET {updates}'
        )
        try:
            async with self.db_manager.connection_scope() as conn:
                for metric in metrics:
                    values = [getattr(metric, column) for column in columns]
                    values[columns.index("timestamp")] = to_iso_z(metric.timestamp)
                    await conn.execute(query, values)
                await conn.commit()
            return len(metrics)
        except sqlite3.Error as e:
            logging.error("Could not write node idle metrics: %s", e)
            raise QueryError(f"Could not write node idle metrics: {e}") from e

    async def read_idle_metrics(self, start_time: datetime, end_time: datetime) -> List[Any]:
        """Reads ``__idle__`` rows whose timestamp is in ``[start_time, end_time)``."""
        from greenkube.impact.rows import NodeIdleMetric

        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute(
                    f"""
                    SELECT {", ".join(_quote_idle(c) for c in IDLE_METRIC_COLUMNS)} FROM node_idle_metrics
                    WHERE "timestamp" >= ? AND "timestamp" < ?
                    ORDER BY "timestamp", node
                    """,
                    (to_iso_z(start_time), to_iso_z(end_time)),
                ) as cursor:
                    rows = await cursor.fetchall()
            result = []
            for row in rows:
                data = dict(row)
                data["timestamp"] = ensure_utc(data["timestamp"])
                result.append(NodeIdleMetric(**data))
            return result
        except sqlite3.Error as e:
            logging.error("Could not read node idle metrics: %s", e)
            raise QueryError(f"Could not read node idle metrics: {e}") from e

    async def read_container_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[CombinedMetric]:
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                if namespace:
                    query = """
                        SELECT * FROM container_metrics
                        WHERE timestamp >= ? AND timestamp < ? AND namespace = ?
                        ORDER BY timestamp
                    """
                    params = (to_iso_z(start_time), to_iso_z(end_time), namespace)
                else:
                    query = """
                        SELECT * FROM container_metrics
                        WHERE timestamp >= ? AND timestamp < ?
                        ORDER BY timestamp
                    """
                    params = (to_iso_z(start_time), to_iso_z(end_time))
                async with conn.execute(query, params) as cursor:
                    return [_container_metric_from_row(row) for row in await cursor.fetchall()]
        except sqlite3.Error as e:
            logging.error("Could not read container metrics: %s", e)
            raise QueryError(f"Could not read container metrics: {e}") from e

    async def read_hourly_container_metrics(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[CombinedMetric]:
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                if namespace:
                    query = """
                        SELECT * FROM container_metrics_hourly
                        WHERE hour_bucket >= ? AND hour_bucket < ? AND namespace = ?
                        ORDER BY hour_bucket
                    """
                    params = (to_iso_z(start_time), to_iso_z(end_time), namespace)
                else:
                    query = """
                        SELECT * FROM container_metrics_hourly
                        WHERE hour_bucket >= ? AND hour_bucket < ?
                        ORDER BY hour_bucket
                    """
                    params = (to_iso_z(start_time), to_iso_z(end_time))
                async with conn.execute(query, params) as cursor:
                    return [_container_metric_from_row(row, hourly=True) for row in await cursor.fetchall()]
        except sqlite3.Error as e:
            logging.error("Could not read hourly container metrics: %s", e)
            raise QueryError(f"Could not read hourly container metrics: {e}") from e

    async def recompute_carbon_with_latest_intensities(
        self,
        carbon_intensity_repo: CarbonIntensityRepository,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> int:
        """Set-based recompute of pod and container carbon figures (A9, BUG-06).

        One ``UPDATE ... FROM`` per table joins each row in ``[start, end)`` to
        the latest measured intensity at or before its normalized lookup time,
        provided that point is younger than ``INTENSITY_MAX_AGE``. Only rows
        whose intensity or ``intensity_source`` changes are written. See the
        base-class docstring for the full semantics.
        """
        from greenkube.core.config import get_config

        cfg = get_config()
        lookup_formats = {"hour": "%Y-%m-%dT%H:00:00Z", "day": "%Y-%m-%dT00:00:00Z"}
        lookup_format = lookup_formats.get(getattr(cfg, "NORMALIZATION_GRANULARITY", "hour"), "%Y-%m-%dT%H:%M:%SZ")
        max_age_seconds = cfg.intensity_max_age.total_seconds()
        prefix = INTENSITY_FALLBACK_REASON_PREFIX
        total = 0
        try:
            async with self.db_manager.connection_scope() as conn:
                for table in ("combined_metrics", "container_metrics"):
                    ns_clause = "AND namespace = ?" if namespace else ""
                    # Legacy flags: drop the intensity-default reason, and clear
                    # is_estimated when it was the only one (rows written by json.dumps).
                    reasons = f"{table}.estimation_reasons"
                    has_fallback_reason = f"""json_valid({reasons}) AND {reasons} LIKE '[%"{prefix}%]'"""
                    other_reasons = f"FROM json_each({reasons}) AS r WHERE r.value NOT LIKE '{prefix}%'"
                    params: list[Any] = [to_iso_z(start_time), to_iso_z(end_time)]
                    if namespace:
                        params.append(namespace)
                    # Positional parameters follow their order of appearance in the statement.
                    params += [max_age_seconds, cfg.JOULES_PER_KWH, cfg.DEFAULT_PUE]
                    await conn.execute(
                        f"""
                        WITH lookup AS (
                            SELECT id AS row_id,
                                   emaps_zone AS zone,
                                   strftime('{lookup_format}', "timestamp") AS lookup_ts
                            FROM {table}
                            WHERE "timestamp" >= ? AND "timestamp" < ? {ns_clause}
                              AND emaps_zone IS NOT NULL
                        ),
                        latest AS (
                            SELECT row_id, zone, lookup_ts,
                                   (SELECT MAX(ci.datetime) FROM carbon_intensity_history AS ci
                                    WHERE ci.zone = lookup.zone AND ci.datetime <= lookup.lookup_ts
                                      AND {measured_intensity_sql("ci")}) AS point_ts
                            FROM lookup
                        ),
                        fresh AS (
                            SELECT latest.row_id,
                                   ci.carbon_intensity AS intensity,
                                   CASE WHEN ci.is_estimated = 0 THEN 'consolidated' ELSE 'realtime' END AS source
                            FROM latest
                            JOIN carbon_intensity_history AS ci
                              ON ci.zone = latest.zone AND ci.datetime = latest.point_ts
                            WHERE (julianday(latest.lookup_ts) - julianday(latest.point_ts)) * 86400.0 <= ?
                        )
                        UPDATE {table}
                        SET grid_intensity = fresh.intensity,
                            co2e_grams = (COALESCE({table}.joules, 0) / ?)
                                         * COALESCE({table}.pue, ?) * fresh.intensity,
                            intensity_source = fresh.source,
                            estimation_reasons = CASE WHEN {has_fallback_reason}
                                THEN (SELECT json_group_array(r.value) {other_reasons})
                                ELSE {reasons} END,
                            is_estimated = CASE WHEN {has_fallback_reason}
                                THEN CASE WHEN EXISTS (SELECT 1 {other_reasons}) THEN {table}.is_estimated ELSE 0 END
                                ELSE {table}.is_estimated END
                        FROM fresh
                        WHERE {table}.id = fresh.row_id
                          AND ({table}.grid_intensity IS NOT fresh.intensity
                               OR {table}.intensity_source IS NOT fresh.source)
                        """,
                        params,
                    )
                    # cursor.rowcount is -1 for WITH-prefixed statements; changes() is exact.
                    async with conn.execute("SELECT changes()") as changes_cursor:
                        total += int((await changes_cursor.fetchone())[0] or 0)
                await conn.commit()
            return total
        except sqlite3.Error as e:
            logging.error("Could not recompute carbon figures: %s", e)
            raise QueryError(f"Could not recompute carbon figures: {e}") from e

    async def list_namespaces(self) -> List[str]:
        """Return namespaces from the cache table (fast, no table scan)."""
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                async with conn.execute("SELECT namespace FROM namespace_cache ORDER BY namespace") as cursor:
                    rows = await cursor.fetchall()
                    if rows:
                        return [row["namespace"] for row in rows]

                # Fallback: scan recent combined_metrics if cache is empty
                from datetime import timedelta
                from datetime import timezone as tz

                recent_cutoff = to_iso_z(datetime.now(tz.utc) - timedelta(days=7))
                async with conn.execute(
                    """
                    SELECT DISTINCT namespace FROM combined_metrics
                    WHERE "timestamp" > ?
                    ORDER BY namespace
                    """,
                    (recent_cutoff,),
                ) as cursor:
                    rows = await cursor.fetchall()
                    return [row["namespace"] for row in rows]
        except sqlite3.Error:
            return []

    async def list_metric_years(self, namespace: Optional[str] = None) -> List[int]:
        """Return distinct metric years from raw and hourly SQLite tables."""
        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                raw_where = ['"timestamp" IS NOT NULL']
                hourly_where = ["hour_bucket IS NOT NULL"]
                params: list[str] = []
                if namespace:
                    raw_where.append("namespace = ?")
                    hourly_where.append("namespace = ?")
                    params.extend([namespace, namespace])

                query = f"""
                    SELECT DISTINCT year FROM (
                        SELECT CAST(strftime('%Y', "timestamp") AS INTEGER) AS year
                        FROM combined_metrics
                        WHERE {" AND ".join(raw_where)}
                        UNION
                        SELECT CAST(strftime('%Y', hour_bucket) AS INTEGER) AS year
                        FROM combined_metrics_hourly
                        WHERE {" AND ".join(hourly_where)}
                    )
                    WHERE year IS NOT NULL
                    ORDER BY year DESC
                """
                async with conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [int(row["year"]) for row in rows]
        except sqlite3.Error:
            return []

    async def aggregate_by_namespace(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
    ) -> List[dict]:
        """SQL-level aggregation grouped by namespace — raw + hourly tables."""
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)

        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                parts: list[str] = []
                params: list = []

                if end_time >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_time, cutoff - timedelta(minutes=1))
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(f"""
                        SELECT namespace, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics
                        WHERE "timestamp" >= ? AND "timestamp" < ?{ns_clause}
                    """)
                    params.extend([to_iso_z(raw_start), to_iso_z(end_time)])
                    if namespace:
                        params.append(namespace)

                if start_time < cutoff:
                    hourly_end = min(end_time, cutoff)
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(f"""
                        SELECT namespace, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ? AND hour_bucket < ?{ns_clause}
                    """)
                    params.extend([to_iso_z(start_time), to_iso_z(hourly_end)])
                    if namespace:
                        params.append(namespace)

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
                    FROM ({union_query})
                    GROUP BY namespace
                    ORDER BY co2e_grams DESC
                """
                async with conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error("aggregate_by_namespace failed: %s", e)
            raise QueryError(f"aggregate_by_namespace failed: {e}") from e

    async def aggregate_top_pods(
        self,
        start_time: datetime,
        end_time: datetime,
        namespace: Optional[str] = None,
        limit: int = 10,
    ) -> List[dict]:
        """SQL-level aggregation of top pods by CO2 — raw + hourly tables."""
        from datetime import timedelta
        from datetime import timezone as tz

        from greenkube.core.config import get_config

        cfg = get_config()
        now = datetime.now(tz.utc)
        cutoff = now - timedelta(hours=cfg.METRICS_COMPRESSION_AGE_HOURS)

        try:
            async with self.db_manager.connection_scope() as conn:
                conn.row_factory = aiosqlite.Row
                parts: list[str] = []
                params: list = []

                if end_time >= cutoff - timedelta(minutes=1):
                    raw_start = max(start_time, cutoff - timedelta(minutes=1))
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(f"""
                        SELECT namespace, pod_name, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics
                        WHERE "timestamp" >= ? AND "timestamp" < ?{ns_clause}
                    """)
                    params.extend([to_iso_z(raw_start), to_iso_z(end_time)])
                    if namespace:
                        params.append(namespace)

                if start_time < cutoff:
                    hourly_end = min(end_time, cutoff)
                    ns_clause = " AND namespace = ?" if namespace else ""
                    parts.append(f"""
                        SELECT namespace, pod_name, co2e_grams, embodied_co2e_grams, total_cost, joules
                        FROM combined_metrics_hourly
                        WHERE hour_bucket >= ? AND hour_bucket < ?{ns_clause}
                    """)
                    params.extend([to_iso_z(start_time), to_iso_z(hourly_end)])
                    if namespace:
                        params.append(namespace)

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
                    FROM ({union_query})
                    GROUP BY namespace, pod_name
                    ORDER BY co2e_grams DESC
                    LIMIT ?
                """
                params.append(limit)
                async with conn.execute(query, params) as cursor:
                    rows = await cursor.fetchall()
                    return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logging.error("aggregate_top_pods failed: %s", e)
            raise QueryError(f"aggregate_top_pods failed: {e}") from e

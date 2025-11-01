# src/greenkube/cli.py
"""
This module provides the command-line interface (CLI) for GreenKube,
powered by the Typer library.

It orchestrates the collection, processing, and reporting of FinGreenOps data.
"""
import typer
from typing_extensions import Annotated
from typing import Optional
from datetime import datetime, timezone, timedelta
import time
import logging
import traceback

# --- GreenKube Core Imports ---
from .core.scheduler import Scheduler
from .core.config import config
from .core.calculator import CarbonCalculator
from .core.processor import DataProcessor
from .core.recommender import Recommender

# --- GreenKube Collector Imports ---
from .collectors.electricity_maps_collector import ElectricityMapsCollector
from .collectors.node_collector import NodeCollector
from .collectors.prometheus_collector import PrometheusCollector
from .collectors.opencost_collector import OpenCostCollector
from .collectors.pod_collector import PodCollector

# --- GreenKube Storage Imports ---
from .storage.base_repository import CarbonIntensityRepository
from .storage.sqlite_repository import SQLiteCarbonIntensityRepository
from .storage.elasticsearch_repository import ElasticsearchCarbonIntensityRepository

# --- GreenKube Reporting and Processing Imports ---
from .reporters.console_reporter import ConsoleReporter
from .utils.mapping_translator import get_emaps_zone_from_cloud_zone
from .energy.estimator import BasicEstimator

# --- Setup Logger ---
logging.basicConfig(level=config.LOG_LEVEL.upper(), format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

from typer.core import TyperGroup
import click
import sys
import subprocess


class HelpOnUnknown(TyperGroup):
    """Custom Click Group that prints dynamic help when an unknown command is used."""
    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except Exception:
            # Print dynamic help and re-raise to keep Click behavior
            try:
                help_cmd()
            except Exception:
                pass
            raise


app = typer.Typer(
    name="greenkube",
    help="Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.",
    add_completion=False,
    cls=HelpOnUnknown,
)

def get_repository() -> CarbonIntensityRepository:
    """
    Factory function to get the appropriate repository based on config.
    """
    if config.DB_TYPE == "elasticsearch":
        logger.info("Using Elasticsearch repository.")
        try:
            from .storage import elasticsearch_repository as es_mod
            es_mod.setup_connection()
        except Exception as e:
            logger.error(f"Failed to setup Elasticsearch connection: {e}")
        return ElasticsearchCarbonIntensityRepository()
    elif config.DB_TYPE == "sqlite":
        logger.info("Using SQLite repository.")
        from .core.db import db_manager
        return SQLiteCarbonIntensityRepository(db_manager.get_connection())
    else:
        raise NotImplementedError(f"Repository for DB_TYPE '{config.DB_TYPE}' not implemented.")

def get_processor() -> DataProcessor:
    """
    Factory function to instantiate and return a fully configured DataProcessor.
    """
    logger.info("Initializing data collectors and processor...")
    try:
        # 1. Get the repository
        repository = get_repository()

        # 2. Instantiate all collectors
        prometheus_collector = PrometheusCollector(settings=config)
        opencost_collector = OpenCostCollector()
        node_collector = NodeCollector()
        pod_collector = PodCollector()

        # 3. Instantiate the calculator and estimator
        carbon_calculator = CarbonCalculator(repository=repository)
        estimator = BasicEstimator(settings=config)

        # 4. Instantiate and return the processor
        processor = DataProcessor(
            prometheus_collector=prometheus_collector,
            opencost_collector=opencost_collector,
            node_collector=node_collector,
            pod_collector=pod_collector,
            repository=repository,
            calculator=carbon_calculator,
            estimator=estimator,
        )
        return processor
    except Exception as e:
        logger.error(f"An error occurred during processor initialization: {e}")
        logger.error("Processor initialization failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


def collect_carbon_intensity_for_all_zones():
    """
    Orchestrates the collection and saving of carbon intensity data.
    """
    logger.info("--- Starting hourly carbon intensity collection task ---")
    try:
        repository = get_repository()
        node_collector = NodeCollector()
        em_collector = ElectricityMapsCollector()
    except Exception as e:
        logger.error(f"Failed to initialize components for intensity collection: {e}")
        return

    # ... (rest of the function is unchanged) ...
    try:
        nodes_zones_map = node_collector.collect() # Renamed variable for clarity
        if not nodes_zones_map:
            logger.warning("No node zones discovered.")
            # Decide if we should try a default zone or stop
            # For now, let's stop if no nodes are found.
            return
    except Exception as e:
         logger.error(f"Failed to collect node zones: {e}")
         return # Stop if node collection fails

    # Extract unique cloud zones from the values of the map
    unique_cloud_zones = set(nodes_zones_map.values())
    emaps_zones = set()
    for cz in unique_cloud_zones:
        emz = get_emaps_zone_from_cloud_zone(cz)
        if emz and emz != "unknown": # Check for None and "unknown"
             emaps_zones.add(emz)
        else:
            logger.warning(f"Could not map cloud zone '{cz}' to an Electricity Maps zone.")

    if not emaps_zones:
        logger.warning("No mappable Electricity Maps zones found based on node discovery.")
        # Optionally, fallback to config.DEFAULT_ZONE if desired
        # emaps_zones = {config.DEFAULT_ZONE}
        # logger.info(f"Falling back to default zone: {config.DEFAULT_ZONE}")
        return # Stop for now if no zones mapped


    # --- Collection and saving logic (unchanged) ---
    for zone in emaps_zones:
        try:
            history_data = em_collector.collect(zone=zone)
            if history_data:
                saved_count = repository.save_history(history_data, zone=zone)
                logger.info(f"Successfully saved {saved_count} new records for zone: {zone}")
            else:
                logger.info(f"No new data to save for zone: {zone}")
        except Exception as e:
            logger.error(f"Failed to process data for zone {zone}: {e}")

    logger.info("--- Finished carbon intensity collection task ---")


@app.command()
def start():
    """
    Initialize the database and start the GreenKube data collection service.
    """
    logger.info("🚀 Initializing GreenKube...")
    try:
        # For SQLite, initialize the DB schema if needed
        if config.DB_TYPE == "sqlite":
            # --- Import db_manager locally for SQLite ---
            from .core.db import db_manager
            # -----------------------------------------
            db_manager.setup_sqlite() # Ensure schema exists
            logger.info("✅ SQLite Database connection successful and schema is ready.")
        # Add checks or initial setup for Elasticsearch if necessary in the future

        scheduler = Scheduler()
        scheduler.add_job(collect_carbon_intensity_for_all_zones, interval_hours=1)

        logger.info("📈 Starting scheduler...")
        logger.info("\nGreenKube is running. Press CTRL+C to exit.")

        logger.info("Running initial data collection for all zones...")
        collect_carbon_intensity_for_all_zones()
        logger.info("Initial collection complete.")

        while True:
            scheduler.run_pending()
            time.sleep(60)

    except KeyboardInterrupt:
        logger.info("\n🛑 Shutting down GreenKube service.")
        raise typer.Exit()
    except Exception as e:
        logger.error(f"❌ An unexpected error occurred during startup: {e}")
        logger.error("Startup failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


@app.command()
def report(
    namespace: Annotated[Optional[str], typer.Option(help="Display a detailed report for a specific namespace.")] = None,
    today: Annotated[bool, typer.Option(help="Report from midnight UTC to now")] = False,
    days: Annotated[int, typer.Option(help="Number of days to include (integer)")] = 0,
    hours: Annotated[int, typer.Option(help="Number of hours to include (integer)")] = 0,
    minutes: Annotated[int, typer.Option(help="Number of minutes to include (integer)")] = 0,
    weeks: Annotated[int, typer.Option(help="Number of weeks to include (integer)")] = 0,
    monthly: Annotated[bool, typer.Option(help="Aggregate results by month (UTC)")] = False,
    yearly: Annotated[bool, typer.Option(help="Aggregate results by year (UTC)")] = False,
    format: Annotated[str, typer.Option(help="Output format when --output is provided (csv|json)")] = 'csv',
    output: Annotated[Optional[str], typer.Option(help="Path to output file (CSV or JSON) when provided")] = None,
):
    """
    Displays a combined report of costs and carbon footprint.

    This command now supports the same range-related flags as `report-range`.
    If any range flag is supplied, it delegates to `report_range` to run
    the range-based flow. Without range flags it behaves like the old `report`.
    """
    # If any range-related option is provided, delegate to report_range
    if any((today, days, hours, minutes, weeks, monthly, yearly, output)):
        # Call the existing report_range implementation to handle ranged reports.
        # Typer will handle rendering and exits, so just forward the values.
        return report_range(namespace=namespace, today=today, days=days, hours=hours, minutes=minutes, weeks=weeks, monthly=monthly, yearly=yearly, format=format, output=output)

    logger.info("Initializing GreenKube FinGreenOps reporting tool...")
    try:
        processor = get_processor()
        console_reporter = ConsoleReporter()

        logger.info("Running the data processing pipeline...")
        combined_data = processor.run() # This now handles internal errors more gracefully

        if not combined_data:
             logger.warning("No combined data was generated by the processor.")
             raise typer.Exit(code=0)

        if namespace:
            logger.info(f"Filtering results for namespace: {namespace}...")
            original_count = len(combined_data)
            combined_data = [item for item in combined_data if item.namespace == namespace]
            if not combined_data:
                logger.warning(f"No data found for namespace '{namespace}' after processing {original_count} total items.")
                raise typer.Exit(code=0) # Exit cleanly, just no data for this namespace

        logger.info("Calling the reporter...")
        console_reporter.report(data=combined_data)

    except typer.Exit:
        raise
    except Exception as e:
        # Catch errors during initialization or processing
        logger.error(f"An error occurred during report generation: {e}")
        logger.error("Report generation failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


@app.command()
def report_range(
    namespace: Annotated[str, typer.Option(help="Namespace filter")] = None,
    today: Annotated[bool, typer.Option(help="Report from midnight UTC to now")] = False,
    days: Annotated[int, typer.Option(help="Number of days to include (integer)")] = 0,
    hours: Annotated[int, typer.Option(help="Number of hours to include (integer)")] = 0,
    minutes: Annotated[int, typer.Option(help="Number of minutes to include (integer)")] = 0,
    weeks: Annotated[int, typer.Option(help="Number of weeks to include (integer)")] = 0,
    monthly: Annotated[bool, typer.Option(help="Aggregate results by month (UTC)")] = False,
    yearly: Annotated[bool, typer.Option(help="Aggregate results by year (UTC)")] = False,
    format: Annotated[str, typer.Option(help="Output format when --output is provided (csv|json)")] = 'csv',
    output: Annotated[Optional[str], typer.Option(help="Path to output file (CSV or JSON) when provided")] = None,
):
    """Produce a simple range report. Use --today to report from UTC midnight."""
    # Validate exclusive flags
    if monthly and yearly:
        raise typer.BadParameter("--monthly and --yearly are mutually exclusive")

    end = datetime.now(timezone.utc)
    if today:
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        # If monthly/yearly flags are used without an explicit numeric range, set sensible defaults
        if monthly and not any((days, hours, minutes, weeks)):
            start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif yearly and not any((days, hours, minutes, weeks)):
            start = end.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            delta = timedelta(days=days, hours=hours, minutes=minutes, weeks=weeks)
            if delta.total_seconds() <= 0:
                raise typer.BadParameter("Please provide a positive time range via --days/--hours/... or use --today")
            start = end - delta

    # Reuse the scripts/report_day.py logic but scoped to CLI
    from greenkube.core.config import config as core_config
    import requests
    from collections import defaultdict
    from greenkube.energy.estimator import BasicEstimator
    from greenkube.models.metrics import CombinedMetric

    # Use Z-suffixed ISO timestamps for Prometheus to avoid +00:00 vs Z mismatches
    def iso_z(dt: datetime) -> str:
        return dt.replace(microsecond=0).astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')

    primary_query = "sum(rate(container_cpu_usage_seconds_total[5m])) by (namespace,pod,container,node)"
    fallback_query = "sum(rate(container_cpu_usage_seconds_total[5m])) by (namespace,pod,node)"
    base = core_config.PROMETHEUS_URL.rstrip('/')
    url = f"{base}/api/v1/query_range"

    # Choose a safe step to avoid Prometheus rejecting very large ranges
    # by requesting too many samples. Prefer configured PROMETHEUS_QUERY_RANGE_STEP
    # when set, otherwise scale step so we request at most max_samples points.
    import math

    def parse_duration_to_seconds(s: str) -> int:
        s = str(s).strip()
        try:
            if s.endswith('s'):
                return int(s[:-1])
            if s.endswith('m'):
                return int(s[:-1]) * 60
            if s.endswith('h'):
                return int(s[:-1]) * 3600
            return int(s)
        except Exception:
            return 60

    cfg_step_str = getattr(core_config, 'PROMETHEUS_QUERY_RANGE_STEP', None) or '60s'
    cfg_step_sec = parse_duration_to_seconds(cfg_step_str)
    duration_sec = max(1, int((end - start).total_seconds()))
    max_points = getattr(core_config, 'PROMETHEUS_QUERY_RANGE_MAX_SAMPLES', 10000)
    # Ensure at most max_points samples
    min_step_needed = int(math.ceil(duration_sec / float(max_points)))
    chosen_step_sec = max(cfg_step_sec, min_step_needed, 1)
    step = f"{chosen_step_sec}s"
    logger.debug(f"Prometheus query range duration: {duration_sec}s, chosen step: {step}, cfg_step: {cfg_step_str}")

    params = {"query": primary_query, "start": iso_z(start), "end": iso_z(end), "step": step}

    try:
        resp = requests.get(url, params=params, timeout=60, verify=core_config.PROMETHEUS_VERIFY_CERTS)
        # If Prometheus returns 400 (bad request) raise to try fallback
        resp.raise_for_status()
        results = resp.json().get('data', {}).get('result', [])
        # If no series found, try fallback query without container label (some setups don't expose it)
        if not results:
            raise ValueError("empty_result")
    except Exception as e:
        # Try fallback when Prometheus rejects the grouped-by-container query
        try:
            params = {"query": fallback_query, "start": iso_z(start), "end": iso_z(end), "step": step}
            resp = requests.get(url, params=params, timeout=60, verify=core_config.PROMETHEUS_VERIFY_CERTS)
            resp.raise_for_status()
            results = resp.json().get('data', {}).get('result', [])
        except Exception:
            # Re-raise original exception for upstream logging
            raise

    # Parse Prometheus range results into per-timestamp pod usage
    # results: list of series; each series has 'metric' and 'values' (list of [ts, val])
    # Build map: ts_float -> {(namespace,pod): usage_sum}
    samples = defaultdict(lambda: defaultdict(float))
    pod_node_map_by_ts = defaultdict(dict)  # ts -> (namespace,pod) -> node

    series_count = 0
    unique_pods = set()
    unique_namespaces = set()

    for series in results:
        series_count += 1
        metric = series.get('metric', {}) or {}
        # Be permissive about label names (some Prometheus setups use different label keys)
        series_ns = metric.get('namespace') or metric.get('kubernetes_namespace') or metric.get('namespace_name')
        pod = metric.get('pod') or metric.get('pod_name') or metric.get('kubernetes_pod_name') or metric.get('container')
        node = metric.get('node') or metric.get('kubernetes_node') or ''
        if not series_ns or not pod:
            # try to infer from alternative labels if available
            continue
        unique_pods.add((series_ns, pod))
        unique_namespaces.add(series_ns)
        for ts_val, val in series.get('values', []):
            try:
                usage = float(val)
            except Exception:
                continue
            try:
                ts_f = float(ts_val)
            except Exception:
                continue
            key = (series_ns, pod)
            samples[ts_f][key] += usage
            pod_node_map_by_ts[ts_f][key] = node

    # Promote to info so it's visible in normal runs
    logger.info(f"Prometheus returned {series_count} series; discovered {len(unique_pods)} unique pods across {len(unique_namespaces)} namespaces")
    # Show a small sample of metric labels for debugging
    if series_count > 0:
        sample_labels = [s.get('metric', {}) for s in results[:5]]
        logger.info(f"Sample Prometheus metric labels (up to 5): {sample_labels}")

    # Prepare processor components to perform accurate estimation and emissions
    processor = get_processor()
    estimator = processor.estimator
    calculator = processor.calculator
    repository = processor.repository
    node_collector = processor.node_collector
    pod_collector = processor.pod_collector

    # Collect node instance types to build profiles
    try:
        node_instance_map = node_collector.collect_instance_types() or {}
    except Exception:
        node_instance_map = {}

    # Helper to get profile for a node (mirrors BasicEstimator logic)
    def profile_for_node(node_name: str):
        inst = node_instance_map.get(node_name)
        if inst:
            profile = estimator.instance_profiles.get(inst)
            if profile:
                return profile
            # support cpu-N labels
            if isinstance(inst, str) and inst.startswith('cpu-'):
                try:
                    cores = int(inst.split('-', 1)[1])
                    default_vcores = estimator.DEFAULT_INSTANCE_PROFILE['vcores']
                    if default_vcores <= 0:
                        per_core_min = estimator.DEFAULT_INSTANCE_PROFILE['minWatts']
                        per_core_max = estimator.DEFAULT_INSTANCE_PROFILE['maxWatts']
                    else:
                        per_core_min = estimator.DEFAULT_INSTANCE_PROFILE['minWatts'] / default_vcores
                        per_core_max = estimator.DEFAULT_INSTANCE_PROFILE['maxWatts'] / default_vcores
                    return {'vcores': cores, 'minWatts': per_core_min * cores, 'maxWatts': per_core_max * cores}
                except Exception:
                    pass
        # fallback
        return estimator.DEFAULT_INSTANCE_PROFILE

    # Collect pod request map for fallback and to include in combined metrics
    try:
        pod_metrics_list = pod_collector.collect()
        pod_request_map = { (p.namespace, p.pod_name): p.cpu_request for p in pod_metrics_list }
        pod_mem_map = { (p.namespace, p.pod_name): p.memory_request for p in pod_metrics_list }
    except Exception:
        pod_request_map = {}
        pod_mem_map = {}

    all_energy_metrics = []
    # For each timestamp, compute energy per pod using estimator-like logic
    for ts_f, pod_map in sorted(samples.items()):
        sample_dt = datetime.fromtimestamp(ts_f, tz=timezone.utc)
        # aggregate pod usages for this sample
        pod_cpu_usage = pod_map  # (ns,pod)->cores
        # Build node mappings and node totals
        node_total_cpu = defaultdict(float)
        node_pod_map = defaultdict(list)  # node -> list of (pod_key, cpu)
        for pod_key, cpu in pod_cpu_usage.items():
            node = pod_node_map_by_ts.get(ts_f, {}).get(pod_key)
            if not node:
                node = ''
            node_total_cpu[node] += cpu
            node_pod_map[node].append((pod_key, cpu))

        # For each node compute node-level power and split to pods
        for node_name, pods_on_node in node_pod_map.items():
            profile = profile_for_node(node_name)
            vcores = profile.get('vcores', 1)
            min_watts = profile.get('minWatts', 1.0)
            max_watts = profile.get('maxWatts', 1.0)
            total_cpu = node_total_cpu.get(node_name, 0.0)
            node_util = (total_cpu / vcores) if vcores > 0 else 0.0
            node_util = min(node_util, 1.0)
            node_power_watts = min_watts + (node_util * (max_watts - min_watts))

            if total_cpu <= 0:
                for pod_key, cpu_cores in pods_on_node:
                    em_namespace, pod = pod_key
                    cpu_utilization = cpu_cores / vcores if vcores > 0 else 0.0
                    cpu_utilization = min(cpu_utilization, 1.0)
                    power_draw_watts = min_watts + (cpu_utilization * (max_watts - min_watts))
                    joules = power_draw_watts * estimator.query_range_step_sec
                    em = {
                        'pod_name': pod,
                        'namespace': em_namespace,
                        'joules': joules,
                        'node': node_name,
                        'timestamp': sample_dt
                    }
                    all_energy_metrics.append(em)
            else:
                for pod_key, cpu_cores in pods_on_node:
                    em_namespace, pod = pod_key
                    share = cpu_cores / total_cpu if total_cpu > 0 else 0.0
                    pod_power = node_power_watts * share
                    joules = pod_power * estimator.query_range_step_sec
                    em = {
                        'pod_name': pod,
                        'namespace': em_namespace,
                        'joules': joules,
                        'node': node_name,
                        'timestamp': sample_dt
                    }
                    all_energy_metrics.append(em)

    # Prefetch intensities per zone/hour and populate calculator cache
    try:
        cloud_zones_map = node_collector.collect() or {}
    except Exception:
        cloud_zones_map = {}

    node_emaps_map = {}
    for node, cz in cloud_zones_map.items():
        emz = get_emaps_zone_from_cloud_zone(cz) or config.DEFAULT_ZONE
        node_emaps_map[node] = emz

    zone_to_metrics = defaultdict(list)
    skipped_carbon = 0
    for em in all_energy_metrics:
        node_name = em['node']
        zone = node_emaps_map.get(node_name, config.DEFAULT_ZONE)
        zone_to_metrics[zone].append(em)

    for zone, metrics in zone_to_metrics.items():
        # For each metric determine normalized hour key and prefetch
        for m in metrics:
            ts = m['timestamp']
            # round to hour
            key_dt = ts.replace(minute=0, second=0, microsecond=0)
            key_dt_utc = key_dt.astimezone(timezone.utc).replace(microsecond=0)
            key_plus = key_dt_utc.isoformat()
            key_z = key_plus.replace('+00:00', 'Z')
            cache_key_plus = (zone, key_plus)
            cache_key_z = (zone, key_z)
            if cache_key_plus not in calculator._intensity_cache and cache_key_z not in calculator._intensity_cache:
                try:
                    intensity = repository.get_for_zone_at_time(zone, key_plus)
                except Exception:
                    intensity = None
                calculator._intensity_cache[cache_key_plus] = intensity
                calculator._intensity_cache[cache_key_z] = intensity

    # Now compute CombinedMetric list using calculator
    combined = []
    # build cost and pod request maps
    try:
        cost_metrics = processor.opencost_collector.collect()
        cost_map = {c.pod_name: c for c in cost_metrics}
    except Exception:
        cost_map = {}

    # Debug: show unique pod names present in raw energy metrics
    try:
        raw_pods = sorted({em['pod_name'] for em in all_energy_metrics})
        logger.info(f"Unique pod names in energy metrics (sample up to 5): {raw_pods[:5]} (total {len(raw_pods)})")
    except Exception:
        pass

    for em in all_energy_metrics:
        pod_name = em['pod_name']
        em_namespace = em['namespace']
        node_name = em['node']
        joules = em['joules']
        ts = em['timestamp']
        zone = node_emaps_map.get(node_name, config.DEFAULT_ZONE)
        try:
            carbon_result = calculator.calculate_emissions(joules=joules, zone=zone, timestamp=ts)
        except Exception:
            carbon_result = None
        if carbon_result is None:
            skipped_carbon += 1

        total_cost = cost_map.get(pod_name).total_cost if cost_map.get(pod_name) else config.DEFAULT_COST
        cpu_req = pod_request_map.get((em_namespace, pod_name), 0)
        mem_req = pod_mem_map.get((em_namespace, pod_name), 0)
        if carbon_result:
            # For monthly/yearly aggregation set the period field instead of mangling pod_name.
            period = None
            if monthly:
                period = ts.strftime('%Y-%m')
            elif yearly:
                period = ts.strftime('%Y')
            combined.append(CombinedMetric(pod_name=pod_name, namespace=em_namespace, period=period, total_cost=total_cost, co2e_grams=carbon_result.co2e_grams, pue=calculator.pue, grid_intensity=carbon_result.grid_intensity, joules=joules, cpu_request=cpu_req, memory_request=mem_req))

    logger.info(f"Built {len(combined)} CombinedMetric items (skipped {skipped_carbon} due to missing carbon data)")
    try:
        combined_pods = sorted({c.pod_name for c in combined})
        logger.info(f"Unique pod names in CombinedMetric (sample up to 5): {combined_pods[:5]} (total {len(combined_pods)})")
    except Exception:
        pass

    # Show some cache keys for intensities
    try:
        cache_keys = list(calculator._intensity_cache.keys())[:5]
        logger.info(f"Calculator intensity cache keys (sample up to 5): {cache_keys}")
    except Exception:
        pass

    # Debug: show a sample of CombinedMetric items (namespace, pod_name, period)
    try:
        sample_combined = [(c.namespace, c.pod_name, getattr(c, 'period', None)) for c in combined[:50]]
        logger.debug(f"Sample CombinedMetric entries (up to 50): {sample_combined}")
    except Exception:
        pass

    # Optionally filter by namespace
    if namespace:
        combined = [c for c in combined if c.namespace == namespace]

    # Use the unaggregated combined list as the final list and let the reporter
    # handle aggregation/display. This avoids subtle key-collapsing bugs.
    final_list = combined

    # Extra diagnostics: sizes of combined and final_list and sample items
    try:
        logger.info(f"Len all_energy_metrics={len(all_energy_metrics)}, len(combined)={len(combined)}")
        logger.info(f"Len final_list after assignment={len(final_list)}")
        sample_final = [(c.namespace, c.pod_name, getattr(c, 'period', None)) for c in final_list[:50]]
        logger.debug(f"Sample final_list entries (up to 50): {sample_final}")
    except Exception:
        pass

    logger.info(f"Computed {len(all_energy_metrics)} energy sample entries and {len(final_list)} aggregated CombinedMetric entries")

    # If output file requested, export
    if output:
        import csv, json
        if format not in ('csv', 'json'):
            raise typer.BadParameter("Unsupported format: choose 'csv' or 'json'")
        if format == 'csv':
            with open(output, 'w', newline='') as f:
                writer = csv.writer(f)
                headers = ['namespace', 'pod_name', 'period', 'joules', 'co2e_grams', 'total_cost', 'cpu_request', 'memory_request']
                writer.writerow(headers)
                for item in final_list:
                    writer.writerow([item.namespace, item.pod_name, item.period or '', f"{item.joules:.6f}", f"{item.co2e_grams:.6f}", f"{item.total_cost:.6f}", item.cpu_request, item.memory_request])
        else:
            with open(output, 'w') as f:
                json.dump([item.model_dump() for item in final_list], f, default=str)
        print(f"Exported report to {output}")

    console = ConsoleReporter()
    # Diagnostic: show how many unique aggregation keys (namespace,pod,period) exist
    try:
        agg_keys = set()
        for c in final_list:
            if getattr(c, 'period', None):
                agg_keys.add((c.namespace, c.pod_name, c.period))
            else:
                agg_keys.add((c.namespace, c.pod_name))
        logger.debug(f"Sample aggregation keys (up to 20): {list(agg_keys)[:20]} (unique {len(agg_keys)})")
    except Exception:
        pass

    console.report(final_list)


@app.command(name="help")
def help_cmd():
    """Show available commands and dynamic usage information.

    This prints the same help text as `greenkube --help` by delegating to
    Click/Typer's help formatter using a synthetic context.
    """
    # First try to run the same module's --help via the current interpreter
    # to ensure we print identical help text to `--help` in the same environment.
    try:
        import os, shutil
        # Prefer the exact executable/script the user invoked (sys.argv[0])
        invoked = sys.argv[0] if len(sys.argv) > 0 else None
        exe = None
        if invoked and os.path.isabs(invoked) and os.path.exists(invoked):
            exe = invoked
        elif invoked and os.path.exists(invoked):
            # relative path
            exe = os.path.abspath(invoked)
        else:
            exe = shutil.which('greenkube')

        if exe:
            proc2 = subprocess.run([exe, '--help'], capture_output=True, text=True, check=False)
            if proc2.returncode == 0 and proc2.stdout:
                print(proc2.stdout)
                return
        # Fallback: try module invocation with current python (may not be executable)
        proc = subprocess.run([sys.executable, '-m', 'greenkube', '--help'], capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout:
            print(proc.stdout)
            return
    except Exception:
        pass

    # Fallback: best-effort listing using Typer's registered_commands metadata
    print("GreenKube available commands:")
    for cmd in getattr(app, 'registered_commands', []):
        name = getattr(cmd, 'name', None) or getattr(cmd, 'callback', None)
        help_text = getattr(cmd, 'help', '') or ''
        # Try to extract a readable name
        if callable(name):
            name = getattr(name, '__name__', str(name))
        print(f" - {name}: {help_text}")
    print('\nUse `greenkube <command> --help` for details on a command.')

@app.command()
def recommend(
    namespace: Annotated[str, typer.Option(
        help="Display recommendations for a specific namespace."
    )] = None
):
    """
    Analyzes data and provides optimization recommendations.
    """
    logger.info("Initializing GreenKube Recommender...")

    try:
        # 1. Get the processed data
        processor = get_processor()
        logger.info("Running the data processing pipeline...")
        combined_data = processor.run()

        if not combined_data:
             logger.warning("No combined data was generated by the processor. Cannot generate recommendations.")
             raise typer.Exit(code=0)

        # 2. Filter by namespace if provided
        if namespace:
            logger.info(f"Filtering results for namespace: {namespace}...")
            combined_data = [item for item in combined_data if item.namespace == namespace]
            if not combined_data:
                logger.warning(f"No data found for namespace '{namespace}'.")
                raise typer.Exit(code=0)

        # 3. Instantiate Recommender and Reporter
        recommender = Recommender() # Uses default thresholds
        console_reporter = ConsoleReporter()

        logger.info("Generating recommendations...")
        
        # 4. Generate recommendations
        zombie_recs = recommender.generate_zombie_recommendations(combined_data)
        rightsizing_recs = recommender.generate_rightsizing_recommendations(combined_data)
        
        all_recs = zombie_recs + rightsizing_recs

        if not all_recs:
            logger.info("\n✅ All systems look optimized! No recommendations to display.")
            return
        # 5. Report recommendations
        logger.info(f"Found {len(all_recs)} recommendations.")
        # Use the unified report method which can accept recommendations
        console_reporter.report(data=combined_data, recommendations=all_recs)

    except typer.Exit:
        raise
    except Exception as e:
        logger.error(f"An error occurred during recommendation generation: {e}")
        logger.error("Recommendation generation failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


@app.command()
def export(
    format: str = typer.Option("csv", help="The output format (e.g., 'csv', 'json')."),
    output: str = typer.Option("report.csv", help="The path to the output file.")
):
    """ Exports the combined report data to a file. (Placeholder) """
    logger.info(f"Placeholder: Exporting data in {format} format to {output}")
    # Implementation would be similar to 'report', but using a file exporter instead of ConsoleReporter

if __name__ == "__main__":
    # Run Typer app but intercept unknown commands to show our dynamic help
    try:
        app()
    except Exception as e:
        # If Click/Typer raised due to unknown command, show dynamic help
        try:
            from click import NoSuchCommand
            if isinstance(e, NoSuchCommand):
                help_cmd()
                raise
        except Exception:
            # Fallback to printing help and re-raising
            help_cmd()
            raise


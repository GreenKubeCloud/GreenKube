# src/greenkube/api/metrics_endpoint.py
"""
Prometheus metrics exposition for the GreenKube API.

``/prometheus/metrics`` serves two registries:

* this module's registry: collection freshness and the savings counters of
  the append-only ledger, refreshed from the database on every scrape;
* the process-local control-loop registry of :mod:`greenkube.core.observability`
  (loops, scans, collection, measurement, ledger health, automation backlog).

Footprint data (cost, CO2e and energy per pod, namespace and node) is served by
the API and the Footprint pages, not as Prometheus series: per-pod and
per-namespace labels multiply the series a Prometheus server has to store.
"""

import logging
import time
import weakref
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from prometheus_client.core import CounterMetricFamily

from greenkube.core.config import get_config
from greenkube.core.observability import generate_latest_observability, record_automation_backlog
from greenkube.models.metrics import CombinedMetric
from greenkube.models.savings import METHOD_MEASURED, METHOD_PROJECTED, SavingsCounterTotal
from greenkube.storage.base_gitops_repository import GitOpsRepository

logger = logging.getLogger(__name__)

# Use a custom registry to avoid polluting the default registry with
# process/platform collectors that are irrelevant in a FastAPI context.
REGISTRY = CollectorRegistry()


def _get_cluster_name() -> str:
    """Return the configured cluster name, falling back to empty string."""
    try:
        cfg = get_config()
        return cfg.CLUSTER_NAME or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Collection freshness
# ---------------------------------------------------------------------------
GREENKUBE_LAST_COLLECTION_TIMESTAMP = Gauge(
    "greenkube_last_collection_timestamp_seconds",
    "Unix timestamp of the most recent metric in the database",
    ["cluster"],
    registry=REGISTRY,
)
GREENKUBE_METRICS_TOTAL = Gauge(
    "greenkube_metrics_total",
    "Number of pods with a stored metric row in the last 20 minutes",
    ["cluster"],
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# Savings ledger metrics (DB-backed)
#
# The ``*_attributed_*_total`` and ``*_reversed_*_total`` families are monotonic
# counters computed from the append-only, hash-chained ledger v2 (LED-04:
# positive entries are attributed, negative entries such as supersession,
# drift and restatement reversals are reversed; the cost of reliability fixes
# is a cost, never a saving, and is left out), labelled by ``method``
# (projected, measured, user_declared). Net savings = attributed − reversed.
# Never sum methods: only ``method="measured"`` is realized savings.
# ---------------------------------------------------------------------------
SAVINGS_COUNTER_LABELS = ["cluster", "recommendation_type", "method"]


class _SavingsLedgerCounterCollector:
    """Expose the latest ledger counter snapshot as Prometheus counters."""

    _FAMILIES = (
        (
            "greenkube_co2e_savings_attributed_grams",
            "Cumulative CO2e (grams) accrued in the savings ledger, by method. Monotonic.",
            "accrued_co2e_grams",
        ),
        (
            "greenkube_cost_savings_attributed_dollars",
            "Cumulative cost (dollars) accrued in the savings ledger, by method; the cost of reliability fixes "
            "is not a saving and is left out. Monotonic.",
            "accrued_cost_dollars",
        ),
        (
            "greenkube_co2e_savings_reversed_grams",
            "Cumulative CO2e (grams) reversed: negative ledger entries (supersession, drift, restatement). Monotonic.",
            "reversed_co2e_grams",
        ),
        (
            "greenkube_cost_savings_reversed_dollars",
            "Cumulative cost reversed: negative savings entries (supersession, drift, restatement); the cost of "
            "reliability fixes is not counted. Monotonic.",
            "reversed_cost_dollars",
        ),
    )

    def __init__(self) -> None:
        self._snapshot: list[tuple[str, SavingsCounterTotal]] = []

    def update(self, cluster: str, counters: Sequence[SavingsCounterTotal]) -> None:
        self._snapshot = [(cluster, counter) for counter in counters]

    def describe(self):
        return [
            CounterMetricFamily(name, documentation, labels=SAVINGS_COUNTER_LABELS)
            for name, documentation, _ in self._FAMILIES
        ]

    def collect(self):
        snapshot = list(self._snapshot)
        for name, documentation, attribute in self._FAMILIES:
            family = CounterMetricFamily(name, documentation, labels=SAVINGS_COUNTER_LABELS)
            for cluster, counter in snapshot:
                family.add_metric(
                    [cluster, counter.recommendation_type, counter.method],
                    max(0.0, float(getattr(counter, attribute))),
                )
            yield family


#: Ledger clusters whose savings metrics already read ledger v2 in this process (the switch is one-way).
_LEDGER_V2_COUNTERS: dict[str, str] = {}


async def savings_counter_source(ledger_repo, ledger_cluster: str) -> Optional[str]:
    """The ledger v2 cluster uid the savings counters read, or None to keep the v1 ledger.

    The counters switch to ledger v2 only once its accrued book reconciles
    with the v1 rows (the projector caught up), so the switch never shows as a
    drop; afterwards they stay on v2 in this process even while the projector
    catches up a new row (review WP-09).
    """
    known = _LEDGER_V2_COUNTERS.get(ledger_cluster)
    if known is not None:
        return known
    identity = await ledger_repo.get_identity(ledger_cluster)
    if identity is None:
        logger.info("Savings ledger v2 has no cluster identity yet: savings metrics read the v1 ledger.")
        return None
    from ..verify.ledger.reconciliation import reconcile

    if not await reconcile(ledger_repo, identity[0], ledger_cluster):
        logger.info("Savings ledger v2 does not reconcile with the v1 ledger yet: savings metrics read the v1 ledger.")
        return None
    _LEDGER_V2_COUNTERS[ledger_cluster] = identity[0]
    return identity[0]


SAVINGS_LEDGER_COUNTERS = _SavingsLedgerCounterCollector()
REGISTRY.register(SAVINGS_LEDGER_COUNTERS)

SAVINGS_MEASURED_RATIO = Gauge(
    "greenkube_savings_measured_vs_projected_ratio",
    "Share of net accrued CO2e savings that is measured rather than projected (0–1), "
    "both accrued on the allocated basis since ledger/2.",
    ["cluster"],
    registry=REGISTRY,
)


# ---------------------------------------------------------------------------
# Update helpers
# ---------------------------------------------------------------------------


def _clear_gauge(gauge: Gauge) -> None:
    """Clear all label combinations from a gauge."""
    gauge._metrics.clear()


# Savings accrue on one-hour slots, so scrapes within this many seconds reuse
# the previous savings refresh instead of scanning the ledger again.
SAVINGS_REFRESH_TTL_SECONDS = 60.0
_savings_refreshed_at: "weakref.WeakKeyDictionary[object, dict[str, float]]" = weakref.WeakKeyDictionary()


def _savings_refresh_due(source: object, key: str) -> bool:
    """Return True when the savings metrics read from ``source`` are stale."""
    try:
        last = _savings_refreshed_at.get(source, {}).get(key)
    except TypeError:
        return True
    return last is None or time.monotonic() - last >= SAVINGS_REFRESH_TTL_SECONDS


def _mark_savings_refreshed(source: object, key: str) -> None:
    try:
        _savings_refreshed_at.setdefault(source, {})[key] = time.monotonic()
    except TypeError:
        pass  # not weak-referenceable: refresh on every scrape


def reset_savings_refresh_cache() -> None:
    """Force the next scrape to reload the savings metrics."""
    _savings_refreshed_at.clear()


def update_collection_freshness_metrics(latest_metrics: Sequence[CombinedMetric]) -> None:
    """Publish the newest stored metric timestamp and the number of pods with a recent row.

    The timestamp keeps its last value when no row is recent, so
    ``time() - greenkube_last_collection_timestamp_seconds`` keeps growing
    while collection is stalled.
    """
    cluster = _get_cluster_name()
    GREENKUBE_METRICS_TOTAL.labels(cluster=cluster).set(len(latest_metrics))
    latest_ts = max((m.timestamp for m in latest_metrics if m.timestamp), default=None)
    if latest_ts:
        GREENKUBE_LAST_COLLECTION_TIMESTAMP.labels(cluster=cluster).set(latest_ts.timestamp())


def update_attributed_savings_metrics(counters: Sequence[SavingsCounterTotal], cluster: str) -> None:
    """Publish the ledger's monotonic counters and the measured share.

    Args:
        counters: Output of ``SavingsLedgerRepository.get_lifetime_counters()``.
        cluster:  Cluster label value.
    """
    SAVINGS_LEDGER_COUNTERS.update(cluster, counters)
    _clear_gauge(SAVINGS_MEASURED_RATIO)
    net: dict[str, float] = defaultdict(float)
    for counter in counters:
        net[counter.method] += counter.accrued_co2e_grams - counter.reversed_co2e_grams
    measured = max(net.get(METHOD_MEASURED, 0.0), 0.0)
    projected = max(net.get(METHOD_PROJECTED, 0.0), 0.0)
    total = measured + projected
    SAVINGS_MEASURED_RATIO.labels(cluster=cluster).set(measured / total if total > 0 else 0.0)
    logger.debug("Updated savings ledger counters for cluster=%s: %d groups.", cluster, len(counters))


def get_metrics_output() -> bytes:
    """Generates the Prometheus text exposition format output.

    Returns:
        Bytes containing the Prometheus text format metrics.
    """
    return generate_latest(REGISTRY) + generate_latest_observability()


async def refresh_metrics_from_db(
    combined_repo,
    savings_repo=None,
    gitops_repository: GitOpsRepository | None = None,
) -> None:
    """Read the database and refresh the API's Prometheus metrics.

    The controller writes data to the database and the API exposes
    ``/prometheus/metrics`` from another process, so this function is called
    on each scrape.

    Args:
        combined_repo: CombinedMetricsRepository instance (collection freshness).
        savings_repo: Savings ledger v1 repository (fallback before ledger v2 reconciles).
        gitops_repository: GitOps repository (automation backlog gauges read by the alerts).
    """
    now = datetime.now(timezone.utc)

    # --- Collection freshness: latest row per pod of the last 20 minutes ---
    try:
        # The controller stores rows on wall-clock steps and may run a few
        # minutes behind; 20 minutes always covers one full collection cycle.
        start = now - timedelta(minutes=20)
        latest_metrics = await combined_repo.read_latest_per_pod(start_time=start, end_time=now)
        update_collection_freshness_metrics(latest_metrics)
    except Exception as exc:
        logger.warning("refresh_metrics_from_db: failed to refresh collection freshness: %s", exc)

    # --- Savings ledger: monotonic counters ---
    if savings_repo is not None:
        try:
            from ..core.savings_attributor import savings_cluster_name

            ledger_cluster = savings_cluster_name()
            cluster = _get_cluster_name()
            ledger_key = f"ledger:{ledger_cluster}:{cluster}"
            if _savings_refresh_due(savings_repo, ledger_key):
                # LED-04: counters come from the hash-chained ledger v2 (restated, reversals
                # visible); the v1 staging ledger only before the ledger has a cluster identity.
                from ..storage.ledger_repository import get_ledger_repository

                ledger_repo = get_ledger_repository()
                ledger_uid = await savings_counter_source(ledger_repo, ledger_cluster)
                if ledger_uid is not None:
                    counters = await ledger_repo.lifetime_counters(ledger_uid)
                else:
                    counters = await savings_repo.get_lifetime_counters(ledger_cluster)
                update_attributed_savings_metrics(counters, cluster=cluster)
                _mark_savings_refreshed(savings_repo, ledger_key)
        except Exception as exc:
            logger.warning("refresh_metrics_from_db: failed to refresh savings ledger metrics: %s", exc)

    if gitops_repository is not None:
        try:
            backlog = await gitops_repository.get_automation_backlog_summary(stale_before=now - timedelta(minutes=15))
            record_automation_backlog(backlog.as_metrics())
        except NotImplementedError:
            logger.debug("refresh_metrics_from_db: GitOps repository does not expose backlog metrics")
        except Exception as exc:
            logger.warning("refresh_metrics_from_db: failed to refresh automation backlog metrics: %s", exc)

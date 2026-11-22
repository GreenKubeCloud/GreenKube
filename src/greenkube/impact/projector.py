# src/greenkube/impact/projector.py
"""Bridges GreenKube's stored metrics to the Impact Model.

Analyzers and connectors call :class:`ImpactProjector` instead of computing
savings themselves (roadmap B4). The projector turns ``CombinedMetric`` rows
and ``NodeInfo`` snapshots into model inputs (samples, node profiles, unit
prices) and delegates every formula to :class:`ImpactModel`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from greenkube.energy.embodied import known_profiles, resolve_node_embodied
from greenkube.energy.node_power import default_profile_from_config, resolve_node_power_profile
from greenkube.energy.shared_host import SharedHost, scale_to_host_share, shared_host_groups
from greenkube.impact.model import DEFAULT_IMPACT_MODEL, ImpactModel
from greenkube.impact.power import memory_watts_per_gib
from greenkube.impact.pricing import (
    CustomRates,
    PricingCatalog,
    UnitPriceResolver,
    cost_split_share,
    observed_rates_from_rows,
    row_hours,
)
from greenkube.impact.realization import (
    SOURCE_CONFIG,
    SOURCE_CONTEXT,
    SOURCE_UNOBSERVED,
    UNOBSERVED_FACTOR,
    current_realization_context,
    realization_override,
)
from greenkube.impact.types import (
    BYTES_PER_GIB,
    HOURS_PER_YEAR,
    AssetDeletion,
    ContainerResourceChange,
    CostSource,
    ImpactDelta,
    NodeImpactProfile,
    NodePoolContext,
    ObservedCoverage,
    PowerSource,
    ResourceSample,
    WorkloadBaseline,
    WorkloadRef,
)
from greenkube.utils.time import parse_query_step_seconds

logger = logging.getLogger(__name__)

#: Attribute used to cache one projector per optimization context.
_CONTEXT_CACHE_ATTR = "_greenkube_impact_projector"

#: Name of the fallback node profile used for samples on unknown nodes.
UNKNOWN_NODE = "__unknown__"

#: ``ObservedCoverage.basis`` values: where a target's first observation came from.
COVERAGE_FROM_SUMMARY = "target_summary"
COVERAGE_FROM_SAMPLES = "samples"
COVERAGE_FROM_LOOKBACK = "lookback"

#: Looks up when a target was first observed (None when unknown).
FirstObservedLookup = Callable[["WorkloadRef"], Optional[datetime]]
#: Returns the collection steps of each hour bucket of the cluster (None or empty when unknown, IMP-04).
CollectedStepsLookup = Callable[[], Optional[Mapping[datetime, float]]]

#: Fallbacks used only when the configuration object lacks a setting.
_FALLBACK_INTENSITY = 500.0
_FALLBACK_PUE = 1.3


def _setting(config: Any, name: str, default: Any) -> Any:
    value = getattr(config, name, default)
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
    elif isinstance(default, str) and not isinstance(value, str):
        return default
    return value


def _positive(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


@dataclass(frozen=True)
class _PowerProfile:
    vcores: float
    min_watts: float
    max_watts: float
    source: PowerSource
    #: vCPUs the profile knows of the node; None for DEFAULT_INSTANCE_PROFILE (NRG-01).
    known_cores: Optional[float] = None


def _resolved_power(
    config: Any,
    name: str,
    instance_type: Optional[str],
    capacity_cores: Optional[float],
    provider: Optional[str],
) -> _PowerProfile:
    """The node power profile of the collected rows (NRG-05): one resolver for both.

    ``resolve_node_power_profile``: instance profile, then capacity × the provider's CCF
    per-vCPU coefficients (their cross-provider average for an unknown provider), then
    ``DEFAULT_INSTANCE_PROFILE``.
    """
    from greenkube.data.instance_profiles import INSTANCE_PROFILES

    power = resolve_node_power_profile(
        node_name=name,
        instance_type=instance_type,
        cpu_capacity_cores=capacity_cores,
        provider=provider,
        instance_profiles=INSTANCE_PROFILES,
        default_profile=default_profile_from_config(config),
    )
    profile = power.profile
    return _PowerProfile(
        vcores=float(profile.get("vcores") or 0) or (capacity_cores or 1.0),
        min_watts=float(profile.get("minWatts", 0.0) or 0.0),
        max_watts=float(profile.get("maxWatts", 0.0) or 0.0),
        source=PowerSource(power.source),
        known_cores=power.known_cores,
    )


def _provider_pue(config: Any, provider: Optional[str]) -> float:
    getter = getattr(config, "get_pue_for_provider", None)
    if callable(getter):
        try:
            value = getter(provider)
            if isinstance(value, (int, float)) and value > 0:
                return float(value)
        except Exception:  # pragma: no cover - defensive against custom configs
            logger.debug("Could not resolve PUE for provider %s", provider)
    return float(_setting(config, "DEFAULT_PUE", _FALLBACK_PUE))


def sample_from_metric(metric: Any, ref: WorkloadRef, default_hours: float) -> ResourceSample:
    """Converts a ``CombinedMetric`` row into a model sample."""

    def _cores(value: Any) -> Optional[float]:
        return float(value) / 1000.0 if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _gib(value: Any) -> Optional[float]:
        return float(value) / BYTES_PER_GIB if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def _cost(name: str) -> Optional[float]:
        value = getattr(metric, name, None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    total_cost = _cost("total_cost")
    hours = row_hours(metric, default_hours)
    split_hours, unsplit_hours, unsplit_total = _cost_split_coverage(metric, hours)
    return ResourceSample(
        ref=ref,
        hours=hours,
        cpu_request_cores=max(_cores(getattr(metric, "cpu_request", 0)) or 0.0, 0.0),
        cpu_usage_cores=_cores(getattr(metric, "cpu_usage_millicores", None)),
        mem_request_gib=max(_gib(getattr(metric, "memory_request", 0)) or 0.0, 0.0),
        mem_usage_gib=_gib(getattr(metric, "memory_usage_bytes", None)),
        node=getattr(metric, "node", None),
        timestamp=getattr(metric, "timestamp", None),
        pue=_positive(getattr(metric, "pue", None)),
        grid_intensity_g_per_kwh=_positive(getattr(metric, "grid_intensity", None)),
        observed_cost_total=total_cost if total_cost is not None and total_cost > 0 else None,
        observed_cost_cpu=_cost("cost_cpu"),
        observed_cost_ram=_cost("cost_ram"),
        observed_cost_storage=_cost("cost_storage"),
        observed_cost_network=_cost("cost_network"),
        observed_cost_lb=_cost("cost_lb"),
        observed_cost_source=getattr(metric, "cost_source", None),
        observed_cost_split_hours=split_hours,
        observed_cost_unsplit_hours=unsplit_hours,
        observed_cost_unsplit_total=unsplit_total,
        gpu_watts=_positive(getattr(metric, "gpu_power_watts", None)),
    )


def _cost_split_coverage(metric: Any, hours: float) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """Split hours, unsplit hours with a cost, and the total of the latter, of an aggregated row (F-03).

    Returns ``(None, None, None)`` when the row says nothing about it (raw
    rows), when the split covers the whole row, or when no hour carries a
    split, so such samples keep the per-sample pricing rules unchanged.
    """
    share = cost_split_share(metric)
    if share is None or share >= 1.0 or share <= 0.0:
        return None, None, None
    duration = float(getattr(metric, "duration_seconds"))
    unsplit_seconds = getattr(metric, "cost_unsplit_duration_seconds", None)
    unsplit_share = (
        min(max(float(unsplit_seconds) / duration, 0.0), 1.0 - share)
        if isinstance(unsplit_seconds, (int, float)) and not isinstance(unsplit_seconds, bool)
        else 1.0 - share
    )
    unsplit_total = getattr(metric, "cost_unsplit_total", None)
    unsplit = float(unsplit_total) if isinstance(unsplit_total, (int, float)) and unsplit_total > 0 else 0.0
    return hours * share, hours * unsplit_share, unsplit


def _utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _is_aggregated(row: Any) -> bool:
    """True for rollup baseline rows, whose timestamp is the last hour they cover, not a sample time."""
    from greenkube.models.workload_usage import BaselineMetric

    return isinstance(row, BaselineMetric)


def first_observed_from_summaries(
    snapshot: Any, ref: WorkloadRef, step_seconds: Optional[float] = None
) -> Optional[datetime]:
    """First observation of a target in a usage snapshot (``UsageSnapshot``), or None.

    A container ref reads its container summary, then the pod-level one; a
    workload ref the pod-level summary, then the earliest of its containers;
    a ``Namespace`` ref the earliest target of the namespace. With
    ``step_seconds`` and the steps of the target's first hour
    (``first_hour_steps``, IMP-04) the first hour counts from the first sample
    (``first_hour + 1 h - steps x step``), not from the hour floor.
    """
    summaries = getattr(snapshot, "summaries", None)
    if not isinstance(summaries, Mapping):
        return None
    if ref.kind == "Namespace":
        candidates = [
            summary
            for target, containers in summaries.items()
            if target and target[0] == ref.namespace
            for summary in containers.values()
        ]
    else:
        containers = summaries.get((ref.namespace, ref.kind, ref.name)) or {}
        candidates = []
        for key in ((ref.container,) if ref.container else ()) + ("",):
            summary = containers.get(key)
            if summary is not None:
                candidates = [summary]
                break
        if not candidates:
            candidates = list(containers.values())
    firsts: List[datetime] = []
    for summary in candidates:
        hour = _utc(getattr(summary, "first_hour", None))
        if hour is None:
            continue
        steps = getattr(summary, "first_hour_steps", 0)
        last = getattr(summary, "first_hour_last_offset", None)
        # The first hour's steps run up to its end only when its latest sample sits in the hour's
        # last step; a target that ran 10:00-10:10 keeps 10:00 (IMP-04 review).
        contiguous = isinstance(last, (int, float)) and step_seconds and last >= 3600 - 1.5 * step_seconds
        if (
            contiguous
            and step_seconds
            and isinstance(steps, int)
            and not isinstance(steps, bool)
            and 0 < steps * step_seconds < 3600
        ):
            hour += timedelta(seconds=3600 - steps * step_seconds)
        firsts.append(hour)
    return min(firsts) if firsts else None


def _context_snapshot_reader(context: Any) -> Optional[Callable[[], Any]]:
    """Reads the context's usage snapshot lazily (``None`` when the context has none)."""
    reader = getattr(context, "usage_snapshot", None)
    if callable(reader):
        return reader
    snapshot = getattr(context, "usage", None)
    if snapshot is None:
        return None
    return lambda: snapshot


def _context_first_observed(context: Any, step_seconds: Optional[float] = None) -> Optional[FirstObservedLookup]:
    """First-observation lookup over the context's usage summaries (computed once, on first use)."""
    reader = _context_snapshot_reader(context)
    if reader is None:
        return None

    def lookup(ref: WorkloadRef) -> Optional[datetime]:
        try:
            snapshot = reader()
        except Exception:  # pragma: no cover - defensive: coverage falls back to samples
            logger.debug("Usage snapshot unavailable for coverage of %s", ref, exc_info=True)
            return None
        return first_observed_from_summaries(snapshot, ref, step_seconds)

    return lookup


def _context_collected_steps(context: Any) -> Optional[CollectedStepsLookup]:
    """The collection steps per hour of the context's usage snapshot (IMP-04), read lazily."""
    reader = _context_snapshot_reader(context)
    if reader is None:
        return None

    def lookup() -> Optional[Mapping[datetime, float]]:
        try:
            snapshot = reader()
        except Exception:  # pragma: no cover - defensive: coverage falls back to the span
            logger.debug("Usage snapshot unavailable for the collection coverage", exc_info=True)
            return None
        steps = getattr(snapshot, "collected_steps_by_hour", None)
        return steps if isinstance(steps, Mapping) and steps else None

    return lookup


def collected_steps_from_rows(rows: Sequence[Any], step_seconds: float) -> Dict[datetime, float]:
    """Collected steps of each UTC hour from metric rows of the whole scope (IMP-04).

    Each distinct row timestamp covers the longest row at that time (one step
    for a raw row, its ``duration_seconds`` for an hourly row read for older
    hours), counted in the hour the row starts and capped at the hour. This is
    the rule of the rollup (``workload_usage_hourly.step_count`` counts the
    steps that start in each hour bucket), so the raw-row and rollup paths give
    the same coverage: collection runs on wall-clock steps, so a step never
    crosses an hour. Empty for rollup baseline rows, whose timestamps are not
    sample times (the usage snapshot gives their collection).
    """
    if step_seconds <= 0:
        return {}
    covered: Dict[datetime, Dict[datetime, float]] = {}
    for row in rows:
        if _is_aggregated(row):
            return {}
        stamp = _utc(getattr(row, "timestamp", None))
        if stamp is None:
            continue
        duration = getattr(row, "duration_seconds", None)
        seconds = float(duration) if isinstance(duration, (int, float)) and duration > 0 else step_seconds
        hour = covered.setdefault(stamp.replace(minute=0, second=0, microsecond=0), {})
        hour[stamp] = max(hour.get(stamp, 0.0), seconds)
    return {hour: min(sum(values.values()), 3600.0) / step_seconds for hour, values in sorted(covered.items())}


def collected_hours(
    steps_by_hour: Mapping[datetime, float], step_seconds: float, start: datetime, end: datetime
) -> float:
    """Hours of ``[start, end)`` in which the collector observed the cluster (IMP-04).

    The span minus the uncollected time of each hour: an hour with no row of
    any target is a gap of its whole overlap with the span, a partly collected
    hour misses its overlap minus its collected time (steps x step), less one
    step of tolerance (the span's ends are not aligned on the step grid). The
    tolerance only applies to an hour in which steps started: an hour without
    any step is a whole gap. A CronJob's idle hours are collected when other
    targets have rows in them.
    """
    if end <= start:
        return 0.0
    steps = {
        bucket: value for bucket, value in ((_utc(hour), value) for hour, value in steps_by_hour.items()) if bucket
    }
    missing = 0.0
    hour = start.replace(minute=0, second=0, microsecond=0)
    while hour < end:
        overlap = (min(hour + timedelta(hours=1), end) - max(hour, start)).total_seconds()
        collected = max(float(steps.get(hour, 0.0)), 0.0) * step_seconds
        tolerance = step_seconds if collected > 0 else 0.0
        missing += max(overlap - collected - tolerance, 0.0)
        hour += timedelta(hours=1)
    return max((end - start).total_seconds() - missing, 0.0) / 3600.0


def observed_window_hours(samples: Sequence[ResourceSample]) -> float:
    """Observation span of samples when the analysis window is unknown."""
    timestamped = [sample for sample in samples if sample.timestamp is not None]
    if timestamped:
        start = min(sample.timestamp for sample in timestamped if sample.timestamp is not None)
        end = max(
            sample.timestamp + timedelta(hours=sample.hours) for sample in timestamped if sample.timestamp is not None
        )
        span = (end - start).total_seconds() / 3600.0
        if span > 0:
            return span
    return sum(max(sample.hours, 0.0) for sample in samples)


class ImpactProjector:
    """Per-scan entry point of the Impact Model for analyzers and connectors."""

    def __init__(
        self,
        config: Any,
        *,
        node_infos: Iterable[Any] = (),
        rows: Iterable[Any] = (),
        analysis_window_seconds: Optional[float] = None,
        model: Optional[ImpactModel] = None,
        catalog: Optional[PricingCatalog] = None,
        window_end: Optional[datetime] = None,
        first_observed: Optional[FirstObservedLookup] = None,
        collected_steps: Optional[CollectedStepsLookup] = None,
    ):
        """``window_end`` is the end of the analysis window and ``first_observed``
        finds when a target was first observed; with them, projections are
        annualized over each target's observed coverage (Impact Model 2.0.0).
        ``collected_steps`` gives the collection steps of each hour of the
        cluster (IMP-04); without it they are read from raw ``rows``. Coverage
        then counts only the hours the collector observed.
        """
        self.config = config
        self.model: ImpactModel = model if model is not None else DEFAULT_IMPACT_MODEL
        self.cluster_uid = str(_setting(config, "CLUSTER_NAME", "") or "")
        step = _setting(config, "PROMETHEUS_QUERY_RANGE_STEP", "5m")
        self.default_hours = parse_query_step_seconds(step) / 3600.0
        self.window_hours = (
            float(analysis_window_seconds) / 3600.0
            if isinstance(analysis_window_seconds, (int, float)) and analysis_window_seconds > 0
            else None
        )
        self.window_end = _utc(window_end)
        self._first_observed = first_observed
        rows = list(rows)
        self._collected_steps = collected_steps
        self._rows_collected: Optional[Dict[datetime, float]] = (
            None if collected_steps is not None else collected_steps_from_rows(rows, self.default_hours * 3600.0)
        )
        self.resolver = UnitPriceResolver(
            catalog=catalog if catalog is not None else PricingCatalog(),
            custom_rates=CustomRates.from_config(config),
            observed=observed_rates_from_rows(rows, self.default_hours),
        )
        self.pool = self._build_pool(list(node_infos or []), rows)

    # ------------------------------------------------------------------ #
    # Construction                                                        #
    # ------------------------------------------------------------------ #

    @classmethod
    def for_context(cls, context: Any) -> "ImpactProjector":
        """Returns the projector of an optimization context, built once per scan."""
        cached = getattr(context, _CONTEXT_CACHE_ATTR, None)
        if isinstance(cached, ImpactProjector):
            return cached
        rows = list(getattr(context, "metrics", None) or []) + list(getattr(context, "container_metrics", None) or [])
        projector = cls(
            context.config,
            node_infos=getattr(context, "node_infos", None) or [],
            rows=rows,
            analysis_window_seconds=getattr(context, "analysis_window_seconds", None),
            window_end=getattr(context, "window_end", None),
            first_observed=_context_first_observed(
                context, parse_query_step_seconds(_setting(context.config, "PROMETHEUS_QUERY_RANGE_STEP", "5m"))
            ),
            collected_steps=_context_collected_steps(context),
        )
        try:
            setattr(context, _CONTEXT_CACHE_ATTR, projector)
        except (AttributeError, TypeError):  # pragma: no cover - frozen or slotted contexts
            pass
        return projector

    def _build_pool(self, node_infos: List[Any], rows: List[Any]) -> NodePoolContext:
        config = self.config
        default_intensity = float(_setting(config, "DEFAULT_INTENSITY", _FALLBACK_INTENSITY))
        env = _row_environment(rows)
        nodes: Dict[str, NodeImpactProfile] = {}
        # Nodes that are one physical host (NRG-15), split like the collected rows.
        named = {getattr(info, "name", None): info for info in node_infos}
        shared_hosts = (
            shared_host_groups({name: info for name, info in named.items() if isinstance(name, str) and name})
            if _setting(config, "SHARED_HOST_DETECTION", True)
            else {}
        )
        for info in node_infos:
            name = getattr(info, "name", None)
            if not isinstance(name, str) or not name:
                continue
            nodes[name] = self._node_profile(
                name=name,
                instance_type=getattr(info, "instance_type", None),
                provider=getattr(info, "cloud_provider", None),
                region=getattr(info, "region", None),
                capacity_cores=_positive(getattr(info, "cpu_capacity_cores", None)),
                memory_bytes=_positive(getattr(info, "memory_capacity_bytes", None)),
                embodied_kg=_positive(getattr(info, "embodied_emissions_kg", None)),
                env=env.get(name),
                default_intensity=default_intensity,
                shared_host=shared_hosts.get(name),
            )

        # Nodes seen in metric rows but absent from the node snapshot keep their
        # instance type (power profile) and observed prices, if any.
        for name, instance_type in _row_instance_types(rows).items():
            if name in nodes:
                continue
            nodes[name] = self._node_profile(
                name=name,
                instance_type=instance_type,
                provider=None,
                region=None,
                capacity_cores=None,
                memory_bytes=None,
                embodied_kg=None,
                env=env.get(name),
                default_intensity=default_intensity,
            )

        default_node = self._node_profile(
            name=UNKNOWN_NODE,
            instance_type=None,
            provider=None,
            region=None,
            capacity_cores=None,
            memory_bytes=None,
            embodied_kg=None,
            env=None,
            default_intensity=default_intensity,
            is_fallback=True,
        )
        override = realization_override(config)
        context = current_realization_context() if override is None else None
        if override is not None:
            factor, source, reasons = override, SOURCE_CONFIG, ("IMPACT_REALIZATION_FACTOR pins the factor",)
        elif context is not None:
            default_pool = context.pools[context.default_pool]
            factor, source, reasons = default_pool.factor, SOURCE_CONTEXT, default_pool.reasons
        else:
            factor, source = UNOBSERVED_FACTOR, SOURCE_UNOBSERVED
            reasons = ("The cluster's autoscalers have not been observed yet: expected savings equal allocated",)
        return NodePoolContext(
            nodes=nodes,
            default_node=default_node,
            realization_factor=factor,
            carbon_price_per_tonne=float(_setting(config, "CARBON_PRICE_PER_TONNE", 100.0)),
            currency=self.resolver.custom_rates.currency,
            realization_source=source,
            realization_reasons=tuple(reasons),
            realization=context,
        )

    def _node_profile(
        self,
        *,
        name: str,
        instance_type: Optional[str],
        provider: Optional[str],
        region: Optional[str],
        capacity_cores: Optional[float],
        memory_bytes: Optional[float],
        embodied_kg: Optional[float],
        env: Optional[Tuple[float, float]],
        default_intensity: float,
        is_fallback: bool = False,
        shared_host: Optional[SharedHost] = None,
    ) -> NodeImpactProfile:
        config = self.config
        power = _resolved_power(config, name, instance_type, capacity_cores, provider)
        cores = capacity_cores or power.vcores
        memory_gib = memory_bytes / BYTES_PER_GIB if memory_bytes else 0.0
        prices = self.resolver.resolve(
            node_name=None if is_fallback else name,
            provider=provider,
            region=region,
            instance_type=instance_type,
            vcpus=cores,
            memory_gib=memory_gib or None,
        )
        if not memory_gib and prices.source == CostSource.PRICING_CATALOG:
            entry = self.resolver.catalog.lookup(provider, region, instance_type)
            memory_gib = entry.memory_gib if entry is not None and entry.memory_gib else 0.0

        # Same embodied resolution as the collected rows (NRG-01): operator value, Boavizta profile
        # known to this process, per-vCPU fallback, flat default.
        embodied = resolve_node_embodied(
            SimpleNamespace(
                embodied_emissions_kg=embodied_kg,
                cloud_provider=provider,
                instance_type=instance_type,
                cpu_capacity_cores=capacity_cores,
                memory_capacity_bytes=memory_bytes,
            ),
            known_profiles(),
            config,
            cpu_capacity_cores=capacity_cores or power.known_cores,
        )
        embodied_g_per_hour = embodied.g_per_hour

        pue = env[0] if env else _provider_pue(config, provider)
        intensity = env[1] if env and env[1] > 0 else default_intensity
        scaled = scale_to_host_share(
            idle_watts=power.min_watts,
            max_watts=power.max_watts,
            memory_watts_per_gib=memory_watts_per_gib(config),
            embodied_g_per_hour=embodied_g_per_hour,
            share=shared_host.share if shared_host is not None else 1.0,
        )
        return NodeImpactProfile(
            name=name,
            cpu_capacity_cores=cores,
            mem_capacity_gib=memory_gib,
            idle_watts=scaled.idle_watts,
            max_watts=scaled.max_watts,
            embodied_g_per_hour=scaled.embodied_g_per_hour,
            prices=prices,
            pue=pue,
            grid_intensity_g_per_kwh=intensity,
            power_source=power.source,
            instance_type=instance_type,
            region=region,
            provider=provider,
            is_fallback=is_fallback,
            memory_watts_per_gib=scaled.memory_watts_per_gib,
            embodied_source=embodied.source,
            embodied_reason=embodied.reason(name),
            host_reason=shared_host.reason(name) if shared_host is not None else None,
        )

    # ------------------------------------------------------------------ #
    # Baselines                                                           #
    # ------------------------------------------------------------------ #

    def workload_ref(
        self,
        namespace: Optional[str],
        kind: str,
        name: str,
        container: Optional[str] = None,
    ) -> WorkloadRef:
        return WorkloadRef(
            cluster_uid=self.cluster_uid,
            namespace=namespace or "",
            kind=kind,
            name=name,
            container=container,
        )

    def baseline(self, ref: WorkloadRef, series: Sequence[Any]) -> WorkloadBaseline:
        series = list(series)
        samples = [sample_from_metric(metric, ref, self.default_hours) for metric in series]
        if self.window_hours is None:
            # No analysis window: the samples' own span is already their coverage.
            return WorkloadBaseline(ref=ref, samples=samples, window_hours=observed_window_hours(samples))
        window = self.window_hours
        return WorkloadBaseline(
            ref=ref, samples=samples, window_hours=window, coverage=self.coverage(ref, series, samples, window)
        )

    def coverage(
        self,
        ref: WorkloadRef,
        series: Sequence[Any],
        samples: Sequence[ResourceSample],
        lookback_hours: float,
    ) -> Optional[ObservedCoverage]:
        """Observed coverage of a target: from its first observation to the end of the window.

        The first observation comes from the target's usage summary, else from
        the timestamps of raw metric rows (rollup baseline rows carry the last
        hour they cover, not a sample time). Without either, or without a
        known window end, the whole lookback is the coverage (basis
        ``lookback``), as before Impact Model 2.0.0: coverage is never
        guessed short, which would inflate the projection.
        """
        if lookback_hours <= 0 or not samples:
            return None
        first: Optional[datetime] = None
        basis = COVERAGE_FROM_LOOKBACK
        if self._first_observed is not None:
            first = _utc(self._first_observed(ref))
            if first is not None:
                basis = COVERAGE_FROM_SUMMARY
        raw = not any(_is_aggregated(row) for row in series)
        stamps = [stamp for stamp in (_utc(sample.timestamp) for sample in samples) if stamp is not None]
        if first is None and raw and stamps:
            first, basis = min(stamps), COVERAGE_FROM_SAMPLES
        # Without the window end, the span since the first observation is
        # unknown (the last sample may predate it): keep the lookback.
        end = self.window_end
        if first is None or end is None:
            return ObservedCoverage(
                lookback_hours=lookback_hours, observed_hours=lookback_hours, basis=COVERAGE_FROM_LOOKBACK
            )
        observed = min(max((end - first).total_seconds() / 3600.0, 0.0), lookback_hours)
        steps = self._collection_steps()
        if not steps:
            return ObservedCoverage(
                lookback_hours=lookback_hours, observed_hours=observed, basis=basis, first_observed=first
            )
        # IMP-04: only the hours the collector observed the cluster count.
        collected = min(collected_hours(steps, self.default_hours * 3600.0, first, end), observed)
        return ObservedCoverage(
            lookback_hours=lookback_hours,
            observed_hours=collected,
            basis=basis,
            first_observed=first,
            span_hours=observed,
            collected_hours=collected,
        )

    def _collection_steps(self) -> Optional[Mapping[datetime, float]]:
        """Collection steps per hour of the cluster: from the usage snapshot, else from raw rows."""
        if self._collected_steps is not None:
            try:
                return self._collected_steps()
            except Exception:  # pragma: no cover - defensive: coverage falls back to the span
                logger.debug("Collection coverage unavailable", exc_info=True)
                return None
        return self._rows_collected or None

    # ------------------------------------------------------------------ #
    # Projections                                                         #
    # ------------------------------------------------------------------ #

    def container_resources(
        self,
        ref: WorkloadRef,
        series: Sequence[Any],
        *,
        cpu_millicores: Optional[Tuple[float, float]] = None,
        memory_bytes: Optional[Tuple[float, float]] = None,
    ) -> ImpactDelta:
        """Projects a request change ``(current, proposed)`` for one container."""
        change = ContainerResourceChange(
            cpu_request_cores=(cpu_millicores[0] / 1000.0, cpu_millicores[1] / 1000.0) if cpu_millicores else None,
            mem_request_gib=(
                (memory_bytes[0] / BYTES_PER_GIB, memory_bytes[1] / BYTES_PER_GIB) if memory_bytes else None
            ),
        )
        return self.model.project(self.baseline(ref, series), change, self.pool)

    def asset_deletion(
        self,
        *,
        asset_kind: str,
        allocated_cost_annual: float,
        component: str,
        cost_source: CostSource,
        detail: str = "",
        storage_gib: float = 0.0,
        load_balancers: int = 0,
    ) -> ImpactDelta:
        """Projects deleting a non-compute asset with a known allocated annual cost.

        ``storage_gib`` and ``load_balancers`` are the capacity the deletion
        releases (WP-08.1), reported apart from its cost.
        """
        ref = self.workload_ref(None, asset_kind, asset_kind)
        change = AssetDeletion(
            asset_kind=asset_kind,
            allocated_cost_annual=allocated_cost_annual,
            component=component,
            cost_source=cost_source,
            detail=detail,
            storage_gib=storage_gib,
            load_balancers=load_balancers,
        )
        return self.model.project(WorkloadBaseline(ref=ref, samples=(), window_hours=HOURS_PER_YEAR), change, self.pool)


def _row_environment(rows: Iterable[Any]) -> Mapping[str, Tuple[float, float]]:
    """Average PUE and grid intensity observed per node, weighted by row count."""
    sums: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for row in rows:
        node = getattr(row, "node", None)
        if not node:
            continue
        pue = _positive(getattr(row, "pue", None))
        intensity = _positive(getattr(row, "grid_intensity", None))
        acc = sums[node]
        if pue is not None:
            acc[0] += pue
            acc[1] += 1
        if intensity is not None:
            acc[2] += intensity
            acc[3] += 1
    environment: Dict[str, Tuple[float, float]] = {}
    for node, (pue_sum, pue_n, intensity_sum, intensity_n) in sums.items():
        if pue_n and intensity_n:
            environment[node] = (pue_sum / pue_n, intensity_sum / intensity_n)
    return environment


def _row_instance_types(rows: Iterable[Any]) -> Mapping[str, Optional[str]]:
    instance_types: Dict[str, Optional[str]] = {}
    for row in rows:
        node = getattr(row, "node", None)
        if not node:
            continue
        instance_type = getattr(row, "node_instance_type", None)
        if node not in instance_types or (instance_type and not instance_types[node]):
            instance_types[node] = instance_type if isinstance(instance_type, str) and instance_type else None
    return instance_types


def impact_fields(delta: ImpactDelta) -> Dict[str, Any]:
    """Recommendation fields derived from an impact delta (API v1 compatibility).

    ``potential_savings_cost`` is the expected billable saving and
    ``potential_savings_co2e_grams`` the expected CO2e saving; CO2e stays None
    when the change is outside the energy model (storage, load balancers).
    """
    return {
        "potential_savings_cost": delta.expected_billable_cost_annual,
        "potential_savings_co2e_grams": delta.co2e_expected_annual_g if delta.co2e_modeled else None,
        "impact": delta.to_dict(),
    }

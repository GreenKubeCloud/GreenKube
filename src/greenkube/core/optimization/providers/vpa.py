# src/greenkube/core/optimization/providers/vpa.py
"""VPA (VerticalPodAutoscaler) recommendation source, with Goldilocks provenance.

Reads recommendation-mode VPAs (``updateMode: Off``) and translates their
container targets into CPU/memory rightsizing recommendations. When this source
is enabled, the engine's arbitration drops the native rightsizing recommendation
for the same workload so VPA advice is never duplicated or contradicted.

Goldilocks (roadmap G1) creates one VPA per workload in namespaces labelled
``goldilocks.fairwinds.com/enabled=true`` and labels it ``creator=Fairwinds``,
``source=goldilocks``. Its advice *is* the VPA recommender's advice, so the
connector reads those VPAs once and emits their signals with
``source_id="goldilocks"`` (``source_ref=<ns>/<vpa>``), every other VPA's with
``source_id="vpa"``; both carry ``engine_family="vpa"`` so consensus counts
one vote per engine, and one VPA object is never two signals. Signals are
emitted per VPA object (a Goldilocks VPA and a user VPA on the same container
are two signals of the same engine) and include increases; the v1
recommendations are unchanged (decreases only, ambiguous targets dropped).
``lowerBound``, ``upperBound`` and ``uncappedTarget`` travel in ``raw``; a
target close to its ``upperBound`` is flagged as a volatility risk hint.

Maturity (roadmap A.5, F-12): a VPA whose recommender has seen less than
``VPA_MIN_SAMPLE_HOURS`` of samples (checkpoint ``firstSampleStart`` to
``lastSampleStart``, else ``totalSamplesCount`` at one sample a minute), or
whose ``LowConfidence`` condition is true, is not a vote: it yields neither a
recommendation nor a signal and is counted in the ``immature_vpas``
diagnostic. When no checkpoint could be read the maturity is unknown: the VPA
still votes and the ``vpa_maturity_unknown`` warning says so.
"""

import logging
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    ClassVar,
    Dict,
    FrozenSet,
    List,
    Optional,
    Protocol,
    Set,
    Tuple,
)

from greenkube.collectors.vpa_collector import VPACollector, VPARecommendation
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.core.optimization.statistics import latest_request_value, resource_savings_ratio
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import Recommendation, RecommendationRecord, RecommendationType
from greenkube.models.metrics import RecommendationSource as Source
from greenkube.signals.adapters import RecommendationSourceAdapter, _reset_completeness
from greenkube.signals.mapping import signal_from_recommendation
from greenkube.signals.model import AlgorithmInfo, Capability, SignalOrigin, SourceSignal
from greenkube.signals.sdk import CollectContext, Diagnostic, SourceBatch, SourceDeps, SourceSettings, SourceTier

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

logger = logging.getLogger(__name__)

#: Source id of the signals of Goldilocks-managed VPAs (roadmap G1).
GOLDILOCKS_SOURCE_ID = "goldilocks"
#: Engine family shared by VPA and Goldilocks signals: one consensus vote.
VPA_ENGINE_FAMILY = "vpa"
#: Labels Goldilocks puts on the VPAs it creates.
GOLDILOCKS_LABELS = {"creator": "Fairwinds", "source": "goldilocks"}
#: Namespace label that opts a namespace into Goldilocks.
GOLDILOCKS_NAMESPACE_LABEL = "goldilocks.fairwinds.com/enabled"
#: A target at or above this share of its ``upperBound`` hints at a volatile workload.
VOLATILE_TARGET_TO_UPPER_RATIO = 0.9

_MIB = 1024 * 1024
#: The VPA recommender samples container usage once a minute.
VPA_SAMPLES_PER_HOUR = 60


def vpa_sample_hours(vpa: VPARecommendation) -> Optional[float]:
    """Hours of samples behind a VPA target (checkpoint span, else sample count); None when unknown."""
    first, last = getattr(vpa, "first_sample_start", None), getattr(vpa, "last_sample_start", None)
    if first is not None and last is not None:
        return max(0.0, (last - first).total_seconds() / 3600.0)
    samples = getattr(vpa, "total_samples", None)
    if samples is not None:
        return samples / VPA_SAMPLES_PER_HOUR
    return None


def vpa_maturity(vpa: VPARecommendation, min_hours: float) -> Optional[bool]:
    """Whether a VPA target is mature enough to vote; None when its maturity cannot be read.

    ``LowConfidence=True`` or ``RecommendationProvided=False`` is immature, as
    is a sample history shorter than ``min_hours``. ``min_hours <= 0`` turns
    the check off.
    """
    if min_hours <= 0:
        return True
    conditions = getattr(vpa, "conditions", None) or {}
    if str(conditions.get("LowConfidence", "")).lower() == "true":
        return False
    if str(conditions.get("RecommendationProvided", "")).lower() == "false":
        return False
    hours = vpa_sample_hours(vpa)
    if hours is None:
        return None
    return hours >= min_hours


def is_goldilocks_vpa(vpa: VPARecommendation) -> bool:
    """Whether a VPA was created by Goldilocks (``creator=Fairwinds``, ``source=goldilocks``)."""
    labels = getattr(vpa, "labels", None) or {}
    return all(str(labels.get(key, "")).lower() == value.lower() for key, value in GOLDILOCKS_LABELS.items())


def vpa_source_id(vpa: Optional[VPARecommendation]) -> str:
    """Source id of a VPA's signals: ``goldilocks`` for Goldilocks VPAs, else ``vpa``."""
    return GOLDILOCKS_SOURCE_ID if vpa is not None and is_goldilocks_vpa(vpa) else Source.VPA.value


def volatility_hint(vpa: VPARecommendation) -> Dict[str, Any]:
    """Per resource: ``target / upperBound`` and whether the target sits close to the upper bound."""
    hint: Dict[str, Any] = {}
    for resource, target, upper in (
        ("cpu", vpa.cpu_target_millicores, vpa.cpu_upper_millicores),
        ("memory", vpa.memory_target_bytes, vpa.memory_upper_bytes),
    ):
        if not target or not upper:
            continue
        ratio = round(target / upper, 3)
        hint[resource] = {"target_to_upper": ratio, "volatile": ratio >= VOLATILE_TARGET_TO_UPPER_RATIO}
    return hint


def vpa_raw(vpa: VPARecommendation) -> Dict[str, Any]:
    """Audit payload of one VPA container recommendation (target, bounds, uncapped target, provenance)."""
    return {
        "vpa": f"{vpa.namespace}/{vpa.vpa_name}",
        "container": vpa.container_name,
        "provenance": GOLDILOCKS_SOURCE_ID if is_goldilocks_vpa(vpa) else "user",
        "target": {"cpu_millicores": vpa.cpu_target_millicores, "memory_bytes": vpa.memory_target_bytes},
        "lowerBound": {"cpu_millicores": vpa.cpu_lower_millicores, "memory_bytes": vpa.memory_lower_bytes},
        "upperBound": {"cpu_millicores": vpa.cpu_upper_millicores, "memory_bytes": vpa.memory_upper_bytes},
        "uncappedTarget": {
            "cpu_millicores": getattr(vpa, "cpu_uncapped_millicores", None),
            "memory_bytes": getattr(vpa, "memory_uncapped_bytes", None),
        },
        "volatility": volatility_hint(vpa),
        "containers": getattr(vpa, "containers", None),
        "maturity": {
            "conditions": dict(getattr(vpa, "conditions", None) or {}),
            "total_samples": getattr(vpa, "total_samples", None),
            "sample_hours": vpa_sample_hours(vpa),
        },
    }


@dataclass(frozen=True)
class _ResourceAdvice:
    """One VPA's target for one resource of one container."""

    namespace: str
    kind: str
    name: str
    container: str
    vpa_ref: str
    resource: str
    current: int
    target: int


class VPACollectorLike(Protocol):
    """Minimal collector contract, so tests can inject fakes."""

    async def collect(self) -> List[VPARecommendation]: ...


def _aggregate_by_container(
    vpas: List[VPARecommendation],
) -> Dict[Tuple[str, str, str, str], Dict]:
    """Combines duplicate VPA entries without merging distinct containers."""
    grouped: Dict[Tuple[str, str, str, str], Dict] = {}
    for vpa in vpas:
        container_name = vpa.container_name
        if not container_name or container_name.strip() != container_name:
            continue
        key = (vpa.namespace, vpa.target_kind, vpa.target_name, container_name)
        entry = grouped.setdefault(
            key,
            {
                "cpu_target": None,
                "cpu_ambiguous": False,
                "memory_target": None,
                "memory_ambiguous": False,
                "vpa_names": [],
            },
        )
        for resource, value in (
            ("cpu", vpa.cpu_target_millicores),
            ("memory", vpa.memory_target_bytes),
        ):
            if value is None:
                continue
            target_key = f"{resource}_target"
            ambiguous_key = f"{resource}_ambiguous"
            current = entry[target_key]
            if current is not None and current != value:
                entry[ambiguous_key] = True
            else:
                entry[target_key] = value
        if vpa.vpa_name and vpa.vpa_name not in entry["vpa_names"]:
            entry["vpa_names"].append(vpa.vpa_name)
    return grouped


class VpaSource(RecommendationSource):
    """Recommendation source backed by VerticalPodAutoscalers."""

    name = "vpa"
    priority = 3

    def __init__(self, config: "Config", collector: Optional[VPACollectorLike] = None):
        self.config = config
        self._collector = collector if collector is not None else VPACollector(config)
        self._available: Optional[bool] = None
        #: VPA container recommendations read by the last ``collect()`` (signals per VPA object).
        self.last_vpas: List[VPARecommendation] = []
        #: VPA objects (``ns/name``) set aside by the last ``collect()`` as not mature yet.
        self.immature_vpas: Set[str] = set()
        #: VPA objects whose maturity the last ``collect()`` could not read (they still vote).
        self.unknown_maturity_vpas: Set[str] = set()

    def _mature(self, vpas: List[VPARecommendation]) -> List[VPARecommendation]:
        """The VPA recommendations mature enough to vote; records the others."""
        min_hours = float(getattr(self.config, "VPA_MIN_SAMPLE_HOURS", 24.0) or 0.0)
        kept: List[VPARecommendation] = []
        for vpa in vpas:
            mature = vpa_maturity(vpa, min_hours)
            ref = f"{vpa.namespace}/{vpa.vpa_name}"
            if mature is False:
                self.immature_vpas.add(ref)
                continue
            if mature is None:
                self.unknown_maturity_vpas.add(ref)
            kept.append(vpa)
        if self.immature_vpas:
            logger.info(
                "Ignoring %d VPA(s) with less than %.0f h of samples or low confidence.",
                len(self.immature_vpas),
                min_hours,
            )
        return kept

    async def is_available(self) -> bool:
        """Detects the VPA CRD once; sources are skipped cleanly when absent.

        Test fakes injected through ``collector`` are considered available.
        """
        if self._available is not None:
            return self._available

        if not isinstance(self._collector, VPACollector):
            self._available = True
            return True

        try:
            from greenkube.collectors.vpa_collector import VPA_GROUP, VPA_PLURAL, VPA_VERSION
            from greenkube.core.k8s_client import get_custom_objects_api

            api = await get_custom_objects_api()
            if api is None:
                self._available = False
                return False
            await api.list_cluster_custom_object(
                group=VPA_GROUP,
                version=VPA_VERSION,
                plural=VPA_PLURAL,
                limit=1,
            )
            self._available = True
        except Exception as exc:
            status = getattr(exc, "status", None)
            if status == 404:
                logger.debug("VerticalPodAutoscaler CRD is not installed; VPA source disabled.")
            else:
                logger.warning("VPA availability check failed: %s", exc)
            self._available = False
        return self._available

    async def collect(self, context: "OptimizationContext") -> List[Recommendation]:
        self.last_vpas = []
        self.immature_vpas = set()
        self.unknown_maturity_vpas = set()
        try:
            vpas = await self._collector.collect()
        except Exception as e:
            logger.warning("VPA collection failed: %s. Proceeding without VPA recommendations.", e)
            return []
        vpas = self._mature(list(vpas or []))
        if not vpas:
            return []
        self.last_vpas = vpas

        target_series = context.target_series()
        projector = ImpactProjector.for_context(context)
        recs: List[Recommendation] = []

        for (ns, target_kind, target_name, container_name), agg in _aggregate_by_container(vpas).items():
            workload_series = target_series.get((ns, target_kind, target_name)) or []
            series = [metric for metric in workload_series if metric.container_name == container_name]
            if not series:
                continue

            current_cpu = latest_request_value(series, "cpu_request")
            current_mem = latest_request_value(series, "memory_request")
            vpa_ref = f"{ns}/{agg['vpa_names'][0]}" if agg["vpa_names"] else ns

            for resource, target, ambiguous, current in (
                ("cpu", agg["cpu_target"], agg["cpu_ambiguous"], current_cpu),
                ("memory", agg["memory_target"], agg["memory_ambiguous"], current_mem),
            ):
                if target is None or ambiguous or not current:
                    continue
                if resource_savings_ratio(current, int(target)) is None:
                    continue
                advice = _ResourceAdvice(
                    ns, target_kind, target_name, container_name, vpa_ref, resource, int(current), int(target)
                )
                recs.append(_recommendation(advice, projector, series))

        return recs

    def object_recommendations(self, context: "OptimizationContext") -> List[Recommendation]:
        """One proposal per VPA object, container and resource read by the last ``collect()``.

        Unlike the v1 output, two VPAs on one container stay apart (one signal
        each) and increases are included: consensus decides. These are never
        persisted as v1 recommendations.
        """
        if not self.last_vpas:
            return []
        target_series = context.target_series()
        projector = ImpactProjector.for_context(context)
        seen: Set[Tuple[str, str, str]] = set()
        recs: List[Recommendation] = []
        for vpa in self.last_vpas:
            container_name = vpa.container_name
            if not container_name or container_name.strip() != container_name or not vpa.vpa_name:
                continue
            vpa_ref = f"{vpa.namespace}/{vpa.vpa_name}"
            workload_series = target_series.get((vpa.namespace, vpa.target_kind, vpa.target_name)) or []
            series = [metric for metric in workload_series if metric.container_name == container_name]
            if not series:
                continue
            for resource, target, field_name in (
                ("cpu", vpa.cpu_target_millicores, "cpu_request"),
                ("memory", vpa.memory_target_bytes, "memory_request"),
            ):
                current = latest_request_value(series, field_name)
                key = (vpa_ref, container_name, resource)
                if target is None or not current or int(target) == int(current) or key in seen:
                    continue
                seen.add(key)
                advice = _ResourceAdvice(
                    vpa.namespace,
                    vpa.target_kind,
                    vpa.target_name,
                    container_name,
                    vpa_ref,
                    resource,
                    int(current),
                    int(target),
                )
                recs.append(_recommendation(advice, projector, series))
        return recs


def _recommendation(advice: _ResourceAdvice, projector: ImpactProjector, series: List[Any]) -> Recommendation:
    """The rightsizing recommendation of one VPA target (decrease or increase)."""
    label = f"{advice.kind} '{advice.name}' container '{advice.container}'"
    ref = projector.workload_ref(advice.namespace, advice.kind, advice.name, advice.container)
    current, target = advice.current, advice.target
    verb, change = ("reducing", "reduction") if target < current else ("increasing", "increase")
    ratio = abs(1.0 - target / current) if current else 0.0
    common: Dict[str, Any] = {
        "pod_name": advice.name,
        "container_name": advice.container,
        "namespace": advice.namespace,
        "scope": "workload",
        "owner_kind": advice.kind,
        "owner_name": advice.name,
        "source": Source.VPA,
        "source_ref": advice.vpa_ref,
        "priority": "medium",
    }
    if advice.resource == "cpu":
        # One container, one resource: the Impact Model prices only the
        # released CPU reservation (BUG-15, BUG-16).
        delta = projector.container_resources(ref, series, cpu_millicores=(current, target))
        relation = "exceeds" if target < current else "is below"
        return Recommendation(
            type=RecommendationType.RIGHTSIZING_CPU,
            description=(
                f"{label} CPU request {current}m {relation} the VPA target {target}m "
                f"({ratio:.0%} {change}). Recommend {verb} to {target}m."
            ),
            reason=(
                f"VPA '{advice.vpa_ref}' recommends {target}m CPU for container "
                f"'{advice.container}'; current request is {current}m."
            ),
            current_cpu_request_millicores=current,
            recommended_cpu_request_millicores=target,
            **common,
            **impact_fields(delta),
        )
    delta = projector.container_resources(ref, series, memory_bytes=(current, target))
    relation = "exceeds the" if target < current else "is below the"
    return Recommendation(
        type=RecommendationType.RIGHTSIZING_MEMORY,
        description=(
            f"{label} memory request {current / _MIB:.0f}MiB {relation} "
            f"VPA target {target / _MIB:.0f}MiB ({ratio:.0%} {change}). "
            f"Recommend {verb} to {target / _MIB:.0f}MiB."
        ),
        reason=(
            f"VPA '{advice.vpa_ref}' recommends {target / _MIB:.0f}MiB memory "
            f"for container '{advice.container}'; current request is "
            f"{current / _MIB:.0f}MiB."
        ),
        current_memory_request_bytes=current,
        recommended_memory_request_bytes=target,
        **common,
        **impact_fields(delta),
    )


async def goldilocks_namespaces() -> Optional[List[str]]:
    """Namespaces opted into Goldilocks (``goldilocks.fairwinds.com/enabled=true``); None when unreadable."""
    try:
        from greenkube.core.k8s_client import get_core_v1_api

        core = await get_core_v1_api()
        if core is None:
            return None
        result = await core.list_namespace(label_selector=f"{GOLDILOCKS_NAMESPACE_LABEL}=true")
    except Exception as exc:
        logger.debug("Could not list Goldilocks namespaces: %s", exc)
        return None
    names: List[str] = []
    for item in getattr(result, "items", None) or []:
        metadata = getattr(item, "metadata", None)
        name = getattr(metadata, "name", None) if metadata is not None else None
        if name:
            names.append(str(name))
    return sorted(names)


NamespaceLister = Callable[[], Awaitable[Optional[List[str]]]]


class VpaSignalSource(RecommendationSourceAdapter):
    """VPA recommendation-mode targets as a Source SDK source (E5), with Goldilocks provenance (G1)."""

    id: ClassVar[str] = "vpa"
    display_name: ClassVar[str] = "Vertical Pod Autoscaler"
    tier: ClassVar[SourceTier] = SourceTier.OSS
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.CONTAINER_REQUESTS})
    legacy_source: ClassVar[Optional[str]] = Source.VPA.value
    algorithm_name: ClassVar[Optional[str]] = "vpa-recommender"
    also_emits: ClassVar[FrozenSet[str]] = frozenset({GOLDILOCKS_SOURCE_ID})
    description: ClassVar[str] = (
        "Container request targets of VPAs in recommendation mode (updateMode: Off); "
        "VPAs created by Goldilocks are reported as source goldilocks."
    )

    def __init__(
        self,
        settings: SourceSettings,
        deps: SourceDeps,
        connector: Optional[RecommendationSource] = None,
        namespace_lister: Optional[NamespaceLister] = None,
    ):
        super().__init__(settings, deps, connector=connector)
        #: Lists the Goldilocks opt-in namespaces (diagnostics only).
        self.namespace_lister: NamespaceLister = namespace_lister or goldilocks_namespaces

    def build_connector(self, deps: SourceDeps) -> RecommendationSource:
        return VpaSource(deps.config)

    def raw_payload(self, rec: Recommendation) -> Optional[Dict[str, Any]]:
        return {
            "vpa": rec.source_ref,
            "container": rec.container_name,
            "target": {
                "cpu_millicores": rec.recommended_cpu_request_millicores,
                "memory_bytes": rec.recommended_memory_request_bytes,
            },
        }

    def _vpa_for(self, rec: Recommendation) -> Optional[VPARecommendation]:
        for vpa in getattr(self.connector, "last_vpas", None) or []:
            if f"{vpa.namespace}/{vpa.vpa_name}" == rec.source_ref and vpa.container_name == rec.container_name:
                return vpa
        return None

    def _raw(self, vpa: Optional[VPARecommendation], rec: Recommendation) -> Dict[str, Any]:
        """Audit payload; ``maturity.state`` is ``unknown`` when no checkpoint said how mature the VPA is.

        The consensus never counts a decrease from a VPA of unknown maturity
        (roadmap A.5).
        """
        if vpa is None:
            return self.raw_payload(rec) or {}
        raw = vpa_raw(vpa)
        unknown = getattr(self.connector, "unknown_maturity_vpas", None) or set()
        state = "unknown" if f"{vpa.namespace}/{vpa.vpa_name}" in unknown else "mature"
        raw["maturity"] = {**raw["maturity"], "state": state}
        return raw

    def _signal(self, rec: Recommendation, ctx: CollectContext, fetched_at: Any, *, supports_v1: bool) -> SourceSignal:
        vpa = self._vpa_for(rec)
        signal = signal_from_recommendation(
            rec,
            source_id=vpa_source_id(vpa),
            source_version=type(self).version,
            cluster_uid=ctx.cluster_uid,
            fetched_at=fetched_at,
            algorithm=AlgorithmInfo(name="vpa-recommender", window=ctx.window),
            observed_window=ctx.window,
            origin=SignalOrigin.ADAPTER,
            raw=self._raw(vpa, rec),
        )
        update: Dict[str, Any] = {"engine_family": VPA_ENGINE_FAMILY}
        hint = volatility_hint(vpa) if vpa is not None else {}
        resource = "cpu" if _type_value(rec) == RecommendationType.RIGHTSIZING_CPU.value else "memory"
        if resource in hint:
            data = dict(signal.evidence.data)
            data["risk_hints"] = {"volatility": hint[resource]}
            update["evidence"] = signal.evidence.model_copy(update={"data": data})
        if not supports_v1:
            update["provenance"] = signal.provenance.model_copy(update={"legacy_fingerprint": None})
        return signal.model_copy(update=update)

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        context = ctx.optimization
        if context is None:
            raise RuntimeError(f"source {self.source_id!r} needs GreenKube's optimization context")
        _reset_completeness(self.connector)
        recommendations = list(await self.connector.collect(context))
        complete = getattr(self.connector, "last_collect_complete", True) is not False
        fetched_at = self.deps.clock()

        signals: List[SourceSignal] = []
        emitted: Set[Tuple[Optional[str], Optional[str], str]] = set()
        v1_fingerprints: Set[Optional[str]] = set()
        for rec in recommendations:
            v1_fingerprints.add(RecommendationRecord.from_recommendation(rec).fingerprint)
            signals.append(self._signal(rec, ctx, fetched_at, supports_v1=True))
            emitted.add((rec.source_ref, rec.container_name, _type_value(rec)))
        per_object = self.connector.object_recommendations(context) if isinstance(self.connector, VpaSource) else []
        for rec in per_object:
            key = (rec.source_ref, rec.container_name, _type_value(rec))
            if key in emitted:
                continue
            emitted.add(key)
            supports = RecommendationRecord.from_recommendation(rec).fingerprint in v1_fingerprints
            signals.append(self._signal(rec, ctx, fetched_at, supports_v1=supports))

        return SourceBatch(
            signals=signals,
            recommendations=recommendations,
            complete=complete,
            diagnostics=await self._diagnostics(),
        )

    async def _diagnostics(self) -> List[Diagnostic]:
        """VPAs skipped by update mode, and the Goldilocks opt-in namespaces (roadmap G1)."""
        diagnostics: List[Diagnostic] = []
        immature = getattr(self.connector, "immature_vpas", None) or set()
        if immature:
            diagnostics.append(
                Diagnostic(
                    code="immature_vpas",
                    message=(
                        "VPAs ignored until mature: less than VPA_MIN_SAMPLE_HOURS of samples "
                        "or LowConfidence (roadmap A.5)"
                    ),
                    count=len(immature),
                )
            )
        unknown = getattr(self.connector, "unknown_maturity_vpas", None) or set()
        if unknown:
            diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="vpa_maturity_unknown",
                    message=(
                        "VPAs whose checkpoint could not be read: their maturity is unknown and they still vote; "
                        "grant get/list on verticalpodautoscalercheckpoints"
                    ),
                    count=len(unknown),
                )
            )
        collector = getattr(self.connector, "_collector", None)
        for mode, count in sorted((getattr(collector, "skipped_by_mode", None) or {}).items()):
            diagnostics.append(
                Diagnostic(
                    code="skipped_vpas",
                    message=f"VPAs skipped: updateMode={mode} (only recommendation mode, Off, is read)",
                    count=count,
                )
            )
        vpas = list(getattr(self.connector, "last_vpas", None) or [])
        goldilocks = {(vpa.namespace, vpa.vpa_name) for vpa in vpas if is_goldilocks_vpa(vpa)}
        if goldilocks:
            diagnostics.append(
                Diagnostic(
                    code="goldilocks_vpas",
                    message="VPAs created by Goldilocks, reported as source goldilocks",
                    count=len(goldilocks),
                )
            )
        try:
            namespaces = await self.namespace_lister()
        except Exception:
            namespaces = None
        if namespaces is None:
            return diagnostics
        if namespaces:
            diagnostics.append(
                Diagnostic(
                    code="goldilocks_namespaces",
                    message=f"namespaces opted into Goldilocks ({GOLDILOCKS_NAMESPACE_LABEL}=true)",
                    count=len(namespaces),
                )
            )
        covered = {namespace for namespace, _name in goldilocks}
        missing = [namespace for namespace in namespaces if namespace not in covered]
        if missing:
            listed = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
            diagnostics.append(
                Diagnostic(
                    level="warning",
                    code="goldilocks_namespace_without_vpas",
                    message=(
                        f"opted into Goldilocks but no Goldilocks VPA in recommendation mode was read: {listed}; "
                        "check that the Goldilocks controller runs"
                    ),
                    count=len(missing),
                )
            )
        outside = sorted(covered - set(namespaces))
        if outside:
            diagnostics.append(
                Diagnostic(
                    code="goldilocks_vpas_outside_opt_in",
                    message="Goldilocks VPAs in namespaces without the opt-in label",
                    count=len(outside),
                )
            )
        return diagnostics


def _type_value(rec: Recommendation) -> str:
    return rec.type.value if isinstance(rec.type, RecommendationType) else str(rec.type)

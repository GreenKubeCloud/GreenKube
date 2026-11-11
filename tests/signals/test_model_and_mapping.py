"""Signal model v2 and the v1 <-> v2 mapping (roadmap E4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import TypeAdapter, ValidationError

from greenkube.models.metrics import (
    Recommendation,
    RecommendationCapability,
    RecommendationRecord,
    RecommendationSource,
    RecommendationType,
)
from greenkube.signals.mapping import SOURCE_FIELDS, recommendation_from_signal, signal_from_recommendation
from greenkube.signals.model import (
    MAX_RAW_BYTES,
    AdvisoryProposal,
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    DeletionProposal,
    HpaProposal,
    Proposal,
    ResourceSpec,
    ScheduleProposal,
    SourceSignal,
    TargetRef,
    TimeWindow,
    trim_raw,
)

CLUSTER = "prod-eu"
MIB = 1024 * 1024


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class TestTargetRef:
    def test_key_is_cluster_qualified_with_group_and_container(self):
        ref = TargetRef.for_kind(CLUSTER, "Deployment", "api", namespace="shop", container="web")
        assert ref.api_version == "apps/v1"
        assert ref.key() == "prod-eu/apps.Deployment/shop/api/web"

    def test_core_and_cluster_scoped_kinds(self):
        assert TargetRef.for_kind(CLUSTER, "Service", "lb", namespace="ns").key() == "prod-eu/Service/ns/lb"
        # A cluster-scoped kind never carries a namespace.
        pv = TargetRef.for_kind(CLUSTER, "PersistentVolume", "pv-1", namespace="ignored")
        assert pv.namespace is None
        assert pv.key() == "prod-eu/PersistentVolume/_/pv-1"
        assert TargetRef.for_kind(CLUSTER, "NodePool", "default").key() == "prod-eu/karpenter.sh.NodePool/_/default"


class TestProposals:
    def test_direction_is_derived_from_the_values(self):
        def proposal(current, proposed):
            return ContainerResourcesProposal(
                container="web",
                current=ResourceSpec(**current),
                proposed=ResourceSpec(**proposed),
                direction="increase",  # ignored: always derived
            )

        assert proposal({"cpu_request_m": 500}, {"cpu_request_m": 200}).direction == "decrease"
        assert proposal({"cpu_request_m": 200}, {"cpu_request_m": 500}).direction == "increase"
        mixed = proposal(
            {"cpu_request_m": 500, "memory_request_bytes": 100}, {"cpu_request_m": 200, "memory_request_bytes": 200}
        )
        assert mixed.direction == "mixed"
        assert proposal({"cpu_request_m": 500}, {"cpu_request_m": 500}).direction == "unchanged"

    def test_proposals_are_a_discriminated_union(self):
        adapter = TypeAdapter(Proposal)
        assert isinstance(adapter.validate_python({"kind": "deletion", "reason": "unused"}), DeletionProposal)
        assert isinstance(
            adapter.validate_python({"kind": "schedule", "sleep_at": "20:00", "wake_at": "07:00"}), ScheduleProposal
        )
        with pytest.raises(ValidationError):
            adapter.validate_python({"kind": "teleport"})

    def test_hpa_bounds_stay_unknown_rather_than_invented(self):
        proposal = HpaProposal()
        assert proposal.min_replicas is None and proposal.max_replicas is None


class TestSourceSignal:
    def _signal(self, **overrides) -> SourceSignal:
        values = dict(
            source_id="vpa",
            target=TargetRef.for_kind(CLUSTER, "Deployment", "api", namespace="shop", container="web"),
            capability=Capability.CONTAINER_REQUESTS,
            kind="RIGHTSIZING_CPU",
            proposal=ContainerResourcesProposal(
                container="web",
                current=ResourceSpec(cpu_request_m=500),
                proposed=ResourceSpec(cpu_request_m=300),
            ),
            algorithm=AlgorithmInfo(name="vpa-recommender"),
            fetched_at=_now(),
        )
        values.update(overrides)
        return SourceSignal(**values)

    def test_signal_id_hashes_source_target_capability_and_proposal_only(self):
        first = self._signal()
        later = self._signal(fetched_at=_now() + timedelta(hours=1), source_ref="shop/api-vpa")
        assert first.signal_id == later.signal_id
        assert len(first.signal_id) == 64

        changed = self._signal(
            proposal=ContainerResourcesProposal(
                container="web", current=ResourceSpec(cpu_request_m=500), proposed=ResourceSpec(cpu_request_m=250)
            )
        )
        assert changed.signal_id != first.signal_id
        assert self._signal(source_id="krr").signal_id != first.signal_id

    def test_signal_id_cannot_be_forged(self):
        assert self._signal(signal_id="forged").signal_id == self._signal().signal_id

    def test_json_round_trip(self):
        signal = self._signal(observed_window=TimeWindow(start=_now() - timedelta(days=14), end=_now()))
        assert SourceSignal.model_validate_json(signal.model_dump_json()) == signal

    def test_unknown_fields_and_bad_confidence_are_rejected(self):
        with pytest.raises(ValidationError):
            self._signal(unexpected=True)
        with pytest.raises(ValidationError):
            self._signal(confidence=1.5)

    def test_raw_payload_is_trimmed_to_a_marker(self):
        small = {"vpa": "shop/api"}
        assert trim_raw(small) == small
        big = {"blob": "x" * (MAX_RAW_BYTES + 10)}
        trimmed = trim_raw(big)
        assert trimmed["truncated"] is True and trimmed["size_bytes"] > MAX_RAW_BYTES


def _impact() -> dict:
    return {"model_version": "1.1.0", "kind": "container_resources", "value_annual": 12.5}


#: One recommendation per v1 type, shaped like what the analyzers and connectors emit.
V1_RECOMMENDATIONS = {
    "cpu_workload": Recommendation(
        pod_name="api",
        container_name="web",
        namespace="shop",
        type=RecommendationType.RIGHTSIZING_CPU,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        description="Reduce CPU",
        reason="p95 is low",
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=200,
        potential_savings_cost=12.5,
        potential_savings_co2e_grams=900.0,
        impact=_impact(),
    ),
    "memory_vpa": Recommendation(
        pod_name="api",
        container_name="web",
        namespace="shop",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        source=RecommendationSource.VPA,
        source_ref="shop/api-vpa",
        description="VPA target",
        current_memory_request_bytes=1024 * MIB,
        recommended_memory_request_bytes=512 * MIB,
    ),
    "memory_increase_pod": Recommendation(
        pod_name="standalone",
        container_name="main",
        namespace="shop",
        type=RecommendationType.RIGHTSIZING_MEMORY,
        scope="pod",
        description="OOM risk",
        current_memory_request_bytes=128 * MIB,
        recommended_memory_request_bytes=256 * MIB,
        potential_savings_cost=-3.0,
    ),
    "zombie": Recommendation(
        pod_name="batch",
        namespace="jobs",
        type=RecommendationType.ZOMBIE_POD,
        scope="workload",
        owner_kind="StatefulSet",
        owner_name="batch",
        priority="high",
        description="Idle workload",
        reason="no CPU for 14 days",
        potential_savings_cost=40.0,
    ),
    "autoscaling": Recommendation(
        pod_name="web",
        namespace="shop",
        type=RecommendationType.AUTOSCALING_CANDIDATE,
        scope="workload",
        owner_kind="Deployment",
        owner_name="web",
        description="Spiky usage",
        current_cpu_request_millicores=750,
    ),
    "off_peak": Recommendation(
        pod_name="web",
        namespace="shop",
        type=RecommendationType.OFF_PEAK_SCALING,
        scope="workload",
        owner_kind="Deployment",
        owner_name="web",
        description="Idle at night",
        cron_schedule="Scale to 0: 22:00-06:00 UTC",
        potential_savings_cost=5.0,
    ),
    "off_peak_free_text": Recommendation(
        pod_name="web",
        namespace="shop",
        type=RecommendationType.OFF_PEAK_SCALING,
        scope="workload",
        owner_kind="Deployment",
        owner_name="web",
        description="Idle at night",
        cron_schedule="0 22 * * 1-5",
    ),
    "idle_namespace": Recommendation(
        namespace="sandbox",
        type=RecommendationType.IDLE_NAMESPACE,
        scope="namespace",
        priority="low",
        description="Idle namespace",
    ),
    "carbon_aware": Recommendation(
        pod_name="train",
        namespace="ml",
        type=RecommendationType.CARBON_AWARE_SCHEDULING,
        scope="workload",
        owner_kind="CronJob",
        owner_name="train",
        priority="low",
        description="Shift to a greener hour",
        potential_savings_co2e_grams=1200.0,
    ),
    "node": Recommendation(
        type=RecommendationType.OVERPROVISIONED_NODE,
        scope="node",
        target_node="ip-10-0-0-1",
        description="Node mostly idle",
    ),
    "nodepool": Recommendation(
        type=RecommendationType.OVERPROVISIONED_NODE,
        capability=RecommendationCapability.NODE_POOL,
        scope="node",
        source=RecommendationSource.KARPENTER,
        source_ref="default",
        target_node="nodepool/default",
        description="Consolidate",
        potential_savings_cost=100.0,
    ),
    "orphaned_pv": Recommendation(
        pod_name="pvc-123",
        type=RecommendationType.ORPHANED_PERSISTENT_VOLUME,
        scope="cluster",
        description="Released PV",
    ),
    "orphaned_lb": Recommendation(
        pod_name="old-lb",
        namespace="edge",
        type=RecommendationType.ORPHANED_LOAD_BALANCER,
        scope="cluster",
        description="LoadBalancer without endpoints",
    ),
}


def _signal_of(rec: Recommendation) -> SourceSignal:
    return signal_from_recommendation(rec, source_id="greenkube.test", cluster_uid=CLUSTER, fetched_at=_now())


class TestV1RoundTrip:
    @pytest.mark.parametrize("name", sorted(V1_RECOMMENDATIONS))
    def test_v1_to_v2_to_v1_is_lossless(self, name):
        rec = V1_RECOMMENDATIONS[name]
        rebuilt = recommendation_from_signal(_signal_of(rec))
        fields = set(SOURCE_FIELDS)
        assert rebuilt.model_dump(mode="json", include=fields) == rec.model_dump(mode="json", include=fields)
        assert RecommendationRecord.from_recommendation(rebuilt).fingerprint == (
            RecommendationRecord.from_recommendation(rec).fingerprint
        )

    @pytest.mark.parametrize(
        "name",
        ["cpu_workload", "memory_vpa", "memory_increase_pod", "autoscaling", "off_peak", "orphaned_lb", "orphaned_pv"],
    )
    def test_flat_fields_are_computed_from_target_and_proposal(self, name):
        # Nothing but the identity-free remainder travels in legacy_fields.
        legacy = _signal_of(V1_RECOMMENDATIONS[name]).provenance.legacy_fields
        assert set(legacy) <= {"priority"}

    def test_capabilities_and_proposals(self):
        cpu = _signal_of(V1_RECOMMENDATIONS["cpu_workload"])
        assert cpu.capability == Capability.CONTAINER_REQUESTS
        assert isinstance(cpu.proposal, ContainerResourcesProposal)
        assert cpu.proposal.direction == "decrease"
        assert cpu.target.key() == "prod-eu/apps.Deployment/shop/api/web"
        assert cpu.source_estimate == {
            "potential_savings_cost": 12.5,
            "potential_savings_co2e_grams": 900.0,
            "impact": _impact(),
        }

        increase = _signal_of(V1_RECOMMENDATIONS["memory_increase_pod"])
        assert isinstance(increase.proposal, ContainerResourcesProposal)
        assert increase.proposal.direction == "increase"
        assert increase.target.kind == "Pod"

        schedule = _signal_of(V1_RECOMMENDATIONS["off_peak"])
        assert schedule.proposal == ScheduleProposal(timezone="UTC", sleep_at="22:00", wake_at="06:00")
        assert isinstance(_signal_of(V1_RECOMMENDATIONS["off_peak_free_text"]).proposal, AdvisoryProposal)

        for name in ("zombie", "idle_namespace", "orphaned_pv", "orphaned_lb"):
            signal = _signal_of(V1_RECOMMENDATIONS[name])
            assert signal.capability == Capability.CLEANUP
            assert isinstance(signal.proposal, DeletionProposal)

        assert _signal_of(V1_RECOMMENDATIONS["idle_namespace"]).target.key() == "prod-eu/Namespace/_/sandbox"
        assert _signal_of(V1_RECOMMENDATIONS["orphaned_lb"]).target.key() == "prod-eu/Service/edge/old-lb"
        assert _signal_of(V1_RECOMMENDATIONS["carbon_aware"]).capability == Capability.PLACEMENT
        assert _signal_of(V1_RECOMMENDATIONS["autoscaling"]).capability == Capability.HORIZONTAL_AUTOSCALING
        nodepool = _signal_of(V1_RECOMMENDATIONS["nodepool"])
        assert nodepool.capability == Capability.NODE_POOL
        assert nodepool.target.kind == "NodePool"
        assert _signal_of(V1_RECOMMENDATIONS["node"]).target.kind == "Node"

    def test_signals_of_different_sources_on_one_issue_share_the_v1_fingerprint(self):
        native = V1_RECOMMENDATIONS["memory_vpa"].model_copy(
            update={
                "source": RecommendationSource.GREENKUBE,
                "source_ref": None,
                "recommended_memory_request_bytes": 600 * MIB,
            }
        )
        vpa_signal = _signal_of(V1_RECOMMENDATIONS["memory_vpa"])
        native_signal = _signal_of(native)
        assert vpa_signal.signal_id != native_signal.signal_id
        assert vpa_signal.target == native_signal.target
        assert vpa_signal.provenance.legacy_fingerprint == native_signal.provenance.legacy_fingerprint

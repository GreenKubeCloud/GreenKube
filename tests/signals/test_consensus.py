"""Consensus engine across sources (roadmap E6)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from greenkube.signals.consensus import (
    ConsensusPolicy,
    ballots_for_signal,
    combine,
    consolidate,
    engine_family,
    parse_policy,
)
from greenkube.signals.model import (
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    DeletionProposal,
    ResourceSpec,
    SourceSignal,
    TargetRef,
)

CLUSTER = "prod-eu"
TARGET = TargetRef.for_kind(CLUSTER, "Deployment", "api", namespace="shop", container="web")


def _cpu_signal(source_id: str, proposed: int, *, current: int = 500, algorithm: str | None = None, **params):
    return SourceSignal(
        source_id=source_id,
        target=TARGET,
        capability=Capability.CONTAINER_REQUESTS,
        kind="RIGHTSIZING_CPU",
        proposal=ContainerResourcesProposal(
            container="web",
            current=ResourceSpec(cpu_request_m=current),
            proposed=ResourceSpec(cpu_request_m=proposed),
        ),
        algorithm=AlgorithmInfo(name=algorithm or source_id, params=params),
        fetched_at=datetime.now(timezone.utc),
    )


def _ballots(*signals):
    return [ballot for signal in signals for ballot in ballots_for_signal(signal)]


def _roadmap_fixture():
    # VPA 300m, KRR 250m, native 280m (roadmap E6 acceptance fixture).
    return _ballots(
        _cpu_signal("vpa", 300, algorithm="vpa-recommender"),
        _cpu_signal("krr", 250, algorithm="krr.simple"),
        _cpu_signal("greenkube.rightsizing_cpu", 280),
    )


class TestPolicies:
    def test_safest_picks_the_least_aggressive_value_with_agreement_0_83(self):
        outcome = combine(_roadmap_fixture(), ConsensusPolicy.SAFEST)

        consensus = outcome.consensus
        assert consensus.value == 300
        assert consensus.resource == "cpu_request_m"
        assert consensus.agreement == pytest.approx(0.83, abs=0.005)
        assert consensus.contested is False
        assert consensus.chosen_from == ["vpa"]
        assert outcome.chosen.source_id == "vpa"
        assert consensus.spread == {"cpu_request_m": {"vpa": 300, "krr": 250, "greenkube.rightsizing_cpu": 280}}
        assert consensus.independent_votes == 3

    def test_safest_is_the_default_policy(self):
        assert combine(_roadmap_fixture()).consensus.value == 300

    def test_median_picks_the_middle_value(self):
        assert combine(_roadmap_fixture(), "median").consensus.value == 280

    def test_median_of_an_even_count_is_the_safer_upper_median(self):
        ballots = _ballots(_cpu_signal("vpa", 300), _cpu_signal("krr", 200))
        assert combine(ballots, ConsensusPolicy.MEDIAN).consensus.value == 300

    def test_weighted_median_follows_the_track_record(self):
        weights = {"vpa": 0.2, "krr": 0.9, "greenkube.rightsizing_cpu": 0.2}
        outcome = combine(
            _roadmap_fixture(), ConsensusPolicy.WEIGHTED, weight_for=lambda ballot: weights[ballot.source_id]
        )
        assert outcome.consensus.value == 250
        assert outcome.consensus.chosen_from == ["krr"]

    def test_weighted_without_track_record_is_the_median(self):
        assert combine(_roadmap_fixture(), ConsensusPolicy.WEIGHTED).consensus.value == 280

    def test_priority_keeps_the_highest_priority_source(self):
        priorities = {"greenkube.rightsizing_cpu": 1, "vpa": 3, "krr": 2}
        ballots = _ballots(
            _cpu_signal("vpa", 200), _cpu_signal("krr", 250), _cpu_signal("greenkube.rightsizing_cpu", 280)
        )
        outcome = combine(ballots, ConsensusPolicy.PRIORITY, priorities=priorities)
        assert outcome.consensus.value == 200
        assert outcome.chosen.source_id == "vpa"

    def test_policy_names_and_aliases(self):
        assert parse_policy("") is ConsensusPolicy.SAFEST
        assert parse_policy(None) is ConsensusPolicy.SAFEST
        assert parse_policy("trust-weighted") is ConsensusPolicy.WEIGHTED
        assert parse_policy("source_priority") is ConsensusPolicy.PRIORITY
        assert parse_policy("MEDIAN") is ConsensusPolicy.MEDIAN
        with pytest.raises(ValueError):
            parse_policy("loudest")


class TestAgreement:
    def test_low_agreement_is_contested(self):
        ballots = _ballots(_cpu_signal("vpa", 400), _cpu_signal("krr", 100))
        consensus = combine(ballots).consensus
        assert consensus.agreement == pytest.approx(0.25)
        assert consensus.contested is True
        assert any("agreement" in reason for reason in consensus.reasons)
        assert consensus.confidence_bonus == 1.0

    def test_opposite_directions_are_contested(self):
        ballots = _ballots(_cpu_signal("vpa", 520, current=500), _cpu_signal("krr", 480, current=500))
        consensus = combine(ballots).consensus
        assert consensus.agreement > 0.9
        assert consensus.contested is True
        assert any("direction" in reason for reason in consensus.reasons)

    def test_close_independent_sources_earn_a_confidence_bonus(self):
        ballots = _ballots(_cpu_signal("vpa", 300), _cpu_signal("krr", 290))
        assert combine(ballots).consensus.confidence_bonus > 1.0

    def test_a_single_source_is_never_contested_nor_rewarded(self):
        consensus = combine(_ballots(_cpu_signal("vpa", 300))).consensus
        assert consensus.agreement == 1.0
        assert consensus.contested is False
        assert consensus.confidence_bonus == 1.0

    def test_summary_reads_like_the_ui(self):
        summary = combine(_roadmap_fixture()).consensus.summary()
        assert summary.startswith("vpa 300m · krr 250m · greenkube.rightsizing_cpu 280m → 300m")
        assert "safest" in summary and "0.83" in summary


class TestEngineFamilies:
    def test_goldilocks_and_vpa_on_the_same_vpa_object_are_one_vote(self):
        vpa = _cpu_signal("vpa", 300, algorithm="vpa-recommender")
        goldilocks = _cpu_signal("goldilocks", 300, algorithm="vpa-recommender")
        assert engine_family(vpa) == engine_family(goldilocks) == "vpa"

        outcome = combine(_ballots(vpa, goldilocks, _cpu_signal("krr", 250)))

        assert outcome.consensus.independent_votes == 2
        assert len(outcome.representatives) == 2
        counted = {vote.source_id: vote.counted for vote in outcome.consensus.votes}
        assert counted["krr"] is True
        assert sum(counted[name] for name in ("vpa", "goldilocks")) == 1
        # Both remain visible as supporting sources.
        assert outcome.consensus.chosen_from == ["vpa", "goldilocks"]

    def test_explicit_engine_family_parameter(self):
        signal = _cpu_signal("acme.vpa-mirror", 300, engine_family="VPA")
        assert engine_family(signal) == "vpa"

    def test_goldilocks_source_id_without_algorithm_is_vpa(self):
        assert engine_family(_cpu_signal("goldilocks", 300)) == "vpa"

    def test_family_duplicates_do_not_tilt_the_median(self):
        ballots = _ballots(
            _cpu_signal("vpa", 300, algorithm="vpa-recommender"),
            _cpu_signal("goldilocks", 300, algorithm="vpa-recommender"),
            _cpu_signal("krr", 200),
            _cpu_signal("greenkube.rightsizing_cpu", 150),
        )
        # Three independent votes (300, 200, 150): the median is 200, not 300.
        assert combine(ballots, ConsensusPolicy.MEDIAN).consensus.value == 200


class TestGrouping:
    def test_a_multi_resource_proposal_votes_per_resource(self):
        signal = SourceSignal(
            source_id="krr",
            target=TARGET,
            capability=Capability.CONTAINER_REQUESTS,
            kind="container_resources",
            proposal=ContainerResourcesProposal(
                container="web",
                current=ResourceSpec(cpu_request_m=500, memory_request_bytes=1024),
                proposed=ResourceSpec(cpu_request_m=250, memory_request_bytes=512),
            ),
            algorithm=AlgorithmInfo(name="krr.simple"),
        )
        ballots = ballots_for_signal(signal)
        assert {(ballot.kind, ballot.value) for ballot in ballots} == {
            ("RIGHTSIZING_CPU", 250),
            ("RIGHTSIZING_MEMORY", 512),
        }
        outcomes = consolidate(ballots + _ballots(_cpu_signal("vpa", 300)))
        assert len(outcomes) == 2
        cpu = next(outcome for outcome in outcomes if outcome.key[2] == "RIGHTSIZING_CPU")
        assert cpu.consensus.value == 300

    def test_categorical_proposals_are_decided_by_independent_support(self):
        def deletion(source_id: str, reason: str) -> SourceSignal:
            return SourceSignal(
                source_id=source_id,
                target=TargetRef.for_kind(CLUSTER, "PersistentVolume", "pv-1"),
                capability=Capability.CLEANUP,
                kind="ORPHANED_PERSISTENT_VOLUME",
                proposal=DeletionProposal(reason=reason),
                algorithm=AlgorithmInfo(name=source_id),
            )

        outcome = combine(_ballots(deletion("greenkube.orphaned_pv", "unbound"), deletion("kubecost", "unbound")))
        assert outcome.consensus.resource is None
        assert outcome.consensus.agreement == 1.0
        assert outcome.consensus.independent_votes == 2

    def test_a_consensus_needs_ballots(self):
        with pytest.raises(ValueError):
            combine([])


class TestConfiguration:
    def test_policy_setting_is_validated_and_normalized(self):
        from greenkube.core.config import Config

        assert Config().RECOMMENDATION_CONSENSUS_POLICY == "safest"
        assert Config(RECOMMENDATION_CONSENSUS_POLICY="Trust-Weighted").RECOMMENDATION_CONSENSUS_POLICY == "weighted"
        with pytest.raises(ValueError):
            Config(RECOMMENDATION_CONSENSUS_POLICY="loudest")

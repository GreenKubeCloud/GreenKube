"""Consensus engine across sources (roadmap E6)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from greenkube.signals.consensus import (
    AGREEMENT_CONFIDENCE_BONUS,
    CONTESTED_CONFIDENCE_PENALTY,
    NATIVE_POLICY,
    ballots_for_signal,
    combine,
    consolidate,
    engine_family,
)
from greenkube.signals.model import (
    AlgorithmInfo,
    Capability,
    ContainerResourcesProposal,
    DeletionProposal,
    ResourceSpec,
    SignalOrigin,
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


class TestNativePolicy:
    """B.3: one policy. The native measured value decides; other sources move the confidence."""

    def test_the_native_value_decides_with_agreement_0_83(self):
        outcome = combine(_roadmap_fixture())

        consensus = outcome.consensus
        assert consensus.policy == NATIVE_POLICY
        assert consensus.value == 280
        assert consensus.resource == "cpu_request_m"
        assert consensus.agreement == pytest.approx(0.83, abs=0.005)
        assert consensus.contested is False
        assert consensus.chosen_from == ["greenkube.rightsizing_cpu"]
        assert outcome.chosen.source_id == "greenkube.rightsizing_cpu"
        assert consensus.spread == {"cpu_request_m": {"vpa": 300, "krr": 250, "greenkube.rightsizing_cpu": 280}}
        assert consensus.independent_votes == 3
        assert consensus.confidence_factor == AGREEMENT_CONFIDENCE_BONUS

    def test_an_external_source_never_moves_the_native_value(self):
        for external in (100, 280, 900):
            ballots = _ballots(_cpu_signal("vpa", external), _cpu_signal("greenkube.rightsizing_cpu", 280))
            assert combine(ballots).consensus.value == 280

    def test_external_sources_that_contest_the_native_value_lower_the_confidence(self):
        # The native analysis lowers the request, VPA raises it: contested, confidence down.
        ballots = _ballots(_cpu_signal("vpa", 520), _cpu_signal("greenkube.rightsizing_cpu", 300))
        consensus = combine(ballots).consensus
        assert consensus.value == 300
        assert consensus.contested is True
        assert consensus.confidence_factor == CONTESTED_CONFIDENCE_PENALTY < 1.0

    def test_without_a_native_ballot_the_least_aggressive_value_is_kept(self):
        # Only reachable without a vote screen: with one, an external source alone is no recommendation (A.5).
        ballots = _ballots(_cpu_signal("vpa", 300), _cpu_signal("krr", 200))
        assert combine(ballots).consensus.value == 300

    def test_several_native_ballots_keep_the_highest(self):
        ballots = _ballots(_cpu_signal("greenkube.rightsizing_cpu", 280), _cpu_signal("greenkube", 300))
        assert combine(ballots).consensus.value == 300


class TestAgreement:
    def test_low_agreement_is_contested(self):
        ballots = _ballots(_cpu_signal("vpa", 400), _cpu_signal("krr", 100))
        consensus = combine(ballots).consensus
        assert consensus.agreement == pytest.approx(0.25)
        assert consensus.contested is True
        assert any("agreement" in reason for reason in consensus.reasons)
        assert consensus.confidence_factor == CONTESTED_CONFIDENCE_PENALTY

    def test_opposite_directions_are_contested(self):
        ballots = _ballots(_cpu_signal("vpa", 520, current=500), _cpu_signal("krr", 480, current=500))
        consensus = combine(ballots).consensus
        assert consensus.agreement > 0.9
        assert consensus.contested is True
        assert any("direction" in reason for reason in consensus.reasons)

    def test_close_independent_sources_earn_a_confidence_bonus(self):
        ballots = _ballots(_cpu_signal("vpa", 300), _cpu_signal("krr", 290))
        assert combine(ballots).consensus.confidence_factor > 1.0

    def test_a_single_source_is_never_contested_nor_rewarded(self):
        consensus = combine(_ballots(_cpu_signal("vpa", 300))).consensus
        assert consensus.agreement == 1.0
        assert consensus.contested is False
        assert consensus.confidence_factor == 1.0

    def test_summary_reads_like_the_ui(self):
        summary = combine(_roadmap_fixture()).consensus.summary()
        assert summary.startswith("vpa 300m · krr 250m · greenkube.rightsizing_cpu 280m → 280m")
        assert "native" in summary and "0.83" in summary


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

    def test_family_duplicates_count_once(self):
        ballots = _ballots(
            _cpu_signal("vpa", 300, algorithm="vpa-recommender"),
            _cpu_signal("goldilocks", 300, algorithm="vpa-recommender"),
            _cpu_signal("krr", 200),
            _cpu_signal("greenkube.rightsizing_cpu", 150),
        )
        consensus = combine(ballots).consensus
        # Three independent votes (300, 200, 150); the native value decides.
        assert consensus.independent_votes == 3
        assert consensus.value == 150


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

        outcome = combine(_ballots(deletion("greenkube.orphaned_pv", "unbound"), deletion("acme", "unbound")))
        assert outcome.consensus.resource is None
        assert outcome.consensus.agreement == 1.0
        assert outcome.consensus.independent_votes == 2

    def test_a_consensus_needs_ballots(self):
        with pytest.raises(ValueError):
            combine([])


class TestConfiguration:
    def test_removed_policy_settings_are_accepted_ignored_and_warned_once(self, monkeypatch, caplog):
        """B.0/B.3: RECOMMENDATION_CONSENSUS_POLICY and RECOMMENDATION_SOURCE_PRIORITY have no effect any more."""
        from greenkube.core import config as config_module

        monkeypatch.setattr(config_module, "_REMOVED_WARNED", set())
        monkeypatch.setenv("RECOMMENDATION_CONSENSUS_POLICY", "median")
        monkeypatch.setenv("RECOMMENDATION_SOURCE_PRIORITY", "vpa,greenkube")
        monkeypatch.setenv("SOURCES_CONFIG", '{"kubecost": {"url": "http://kubecost"}}')
        with caplog.at_level("WARNING", logger="greenkube.core.config"):
            config = config_module.Config()
            config_module.Config()
        warnings = [record.getMessage() for record in caplog.records if "removed setting" in record.getMessage()]
        assert len(warnings) == 1
        for name in ("RECOMMENDATION_CONSENSUS_POLICY", "RECOMMENDATION_SOURCE_PRIORITY", "SOURCES_CONFIG.kubecost"):
            assert name in warnings[0]
        assert not hasattr(config, "RECOMMENDATION_CONSENSUS_POLICY")

    def test_removed_settings_in_the_env_file_are_warned_too(self, tmp_path, monkeypatch, caplog):
        """The .env file Config loads is read as well: a removed setting there is not ignored silently."""
        from greenkube.core import config as config_module

        monkeypatch.setattr(config_module, "_REMOVED_WARNED", set())
        for name in ("RECOMMENDATION_CONSENSUS_POLICY", "RECOMMENDATION_SOURCE_PRIORITY", "SOURCES_CONFIG"):
            monkeypatch.delenv(name, raising=False)
        (tmp_path / ".env").write_text('# legacy\nexport RECOMMENDATION_CONSENSUS_POLICY="weighted"\nOTHER=1\n')
        monkeypatch.chdir(tmp_path)
        with caplog.at_level("WARNING", logger="greenkube.core.config"):
            config_module.Config()
        warnings = [record.getMessage() for record in caplog.records if "removed setting" in record.getMessage()]
        assert len(warnings) == 1
        assert "RECOMMENDATION_CONSENSUS_POLICY" in warnings[0]
        assert "RECOMMENDATION_SOURCE_PRIORITY" not in warnings[0]


class TestEngineFamiliesAndPushCaps:
    def test_the_signal_engine_family_field_is_the_vote_unit(self):
        # G1 sets engine_family on the signal itself; consensus must read it.
        relayed = _cpu_signal("push:relay", 260, algorithm="relay").model_copy(update={"engine_family": "vpa"})
        assert engine_family(relayed) == "vpa"

    def test_one_counted_vote_per_push_client(self):
        first = _cpu_signal("push:ci", 200, algorithm="a").model_copy(update={"engine_family": "a"})
        second = _cpu_signal("push:ci", 210, algorithm="b").model_copy(update={"engine_family": "b"})
        outcome = combine(_ballots(first, second, _cpu_signal("krr", 250)))
        assert outcome.consensus.independent_votes == 2
        assert sorted(vote.source_id for vote in outcome.consensus.votes if vote.counted) == ["krr", "push:ci"]

    def test_a_pushed_ballot_never_stands_for_another_sources_family(self):
        relay = _cpu_signal("push:relay", 900).model_copy(update={"engine_family": "vpa"})
        outcome = combine(_ballots(relay, _cpu_signal("vpa", 300, algorithm="vpa-recommender")))
        assert outcome.consensus.value == 300 and outcome.consensus.independent_votes == 1

    def test_pushed_deletions_alone_are_not_admissible(self):
        def deletion(source_id: str) -> SourceSignal:
            return SourceSignal(
                source_id=source_id,
                target=TargetRef.for_kind(CLUSTER, "Deployment", "old", namespace="shop"),
                capability=Capability.CLEANUP,
                kind="ZOMBIE_POD",
                proposal=DeletionProposal(reason="idle"),
                algorithm=AlgorithmInfo(name=source_id),
            )

        assert consolidate(_ballots(deletion("push:a"), deletion("push:b"))) == []
        [outcome] = consolidate(_ballots(deletion("push:a"), deletion("greenkube.zombie")))
        assert set(outcome.consensus.source_ids) == {"push:a", "greenkube.zombie"}


class TestPushedVoteIndependence:
    """One signals:write key can push as several clients and upload KRR reports:
    every pushed ballot together is at most one independent vote (security hardening)."""

    @staticmethod
    def _pushed(source_id: str, proposed: int, family: str):
        signal = _cpu_signal(source_id, proposed, current=1000, algorithm=family)
        return signal.model_copy(
            update={
                "engine_family": family,
                "provenance": signal.provenance.model_copy(update={"origin": SignalOrigin.PUSH}),
            }
        )

    def test_two_push_clients_are_one_independent_vote(self):
        outcome = combine(_ballots(self._pushed("push:a", 400, "a"), self._pushed("push:b", 400, "b")))

        assert outcome.consensus.independent_votes == 1
        # Both proposals are counted votes of the one pushed vote.
        assert {vote.source_id for vote in outcome.consensus.votes if vote.counted} == {"push:a", "push:b"}
        assert all(vote.pushed for vote in outcome.consensus.votes)

    def test_a_krr_report_and_a_push_client_are_one_independent_vote(self):
        outcome = combine(_ballots(self._pushed("krr", 400, "krr"), self._pushed("push:a", 410, "a")))

        assert outcome.consensus.independent_votes == 1

    def test_a_pushed_vote_still_adds_to_a_non_pushed_source(self):
        outcome = combine(
            _ballots(
                self._pushed("krr", 400, "krr"),
                self._pushed("push:a", 410, "a"),
                _cpu_signal("vpa", 420, current=1000, algorithm="vpa-recommender"),
            )
        )

        assert outcome.consensus.independent_votes == 2

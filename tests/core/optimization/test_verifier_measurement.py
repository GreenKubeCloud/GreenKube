# tests/core/optimization/test_verifier_measurement.py
"""Fixed-horizon measurements: windows, provenance, attribution groups and persistence.

Covers C0.1 (no traffic normalization), C0.3 (provenance-based row selection),
C0.5 (terminal outcomes, explicit re-measurement), C0.10 (attribution groups),
C0.12 (non-overlapping windows) and C0.13 (immutable measurements).
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from greenkube.core.config import Config
from greenkube.core.db import db_manager
from greenkube.core.optimization import verifier as verifier_module
from greenkube.core.optimization.lifecycle import RecommendationLifecycle
from greenkube.core.optimization.verifier import RecommendationVerifier, attribution_groups, window_rates
from greenkube.models.measurement import MeasurementOutcome, QuantityOutcome, SavingsMeasurement
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType
from greenkube.storage.measurement_repository import SavingsMeasurementRepository
from greenkube.storage.sqlite.recommendation_repository import SQLiteRecommendationRepository

from .verification_fixtures import (  # pyrefly: ignore[missing-import]
    ProvenancedMetric,
    WindowedMetricsRepo,
    baseline_of,
    rec_id,
    rows,
)

HOURS_PER_YEAR = 8760.0
WINDOW = timedelta(hours=6)


@pytest.fixture
async def repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    yield SQLiteRecommendationRepository(db_manager)
    await db_manager.close()


@pytest.fixture
def measurements():
    return SavingsMeasurementRepository(db_manager)


def _config() -> Config:
    cfg = Config()
    cfg.VERIFICATION_WINDOW_HOURS = 6
    cfg.VERIFICATION_MIN_SAMPLES = 12
    return cfg


def _record(applied_at: datetime, **overrides) -> RecommendationRecord:
    defaults = dict(
        pod_name="api",
        namespace="prod",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="rightsize",
        scope="workload",
        owner_kind="Deployment",
        owner_name="api",
        status=RecommendationStatus.APPLIED,
        current_cpu_request_millicores=1000,
        recommended_cpu_request_millicores=300,
        current_memory_request_bytes=1024**3,
        recommended_memory_request_bytes=512 * 1024**2,
        potential_savings_cost=500.0,
        potential_savings_co2e_grams=5000.0,
        applied_at=applied_at,
        application_method="pr_merge",
        baseline={"restart_count": 0},
        verification_status="pending",
    )
    defaults.update(overrides)
    return RecommendationRecord(**defaults)


async def _all(repo) -> list[RecommendationRecord]:
    records = await repo.get_recommendations_by_statuses([status.value for status in RecommendationStatus])
    return sorted(records, key=lambda record: record.id or 0)


def _verifier(repo, metrics, *, clock=None, health=None) -> RecommendationVerifier:
    return RecommendationVerifier(
        RecommendationLifecycle(repo),
        metrics,  # pyrefly: ignore[bad-argument-type]
        config=_config(),
        health_collector=health,
        clock=clock,
    )


def _timeline():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    applied_at = now - timedelta(hours=8)
    return now, applied_at


class TestPersistence:
    async def test_realized_measurement_is_persisted_and_linked(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, applied_at, cost=0.01) + rows(applied_at, now, cost=0.008),
        )

        updated = await _verifier(repo, metrics).verify_due()

        assert [record.status for record in updated] == [RecommendationStatus.VERIFIED]
        record = updated[0]
        stored = await measurements.list_for_recommendation(record.id)
        assert len(stored) == 1
        measurement = stored[0]
        assert measurement.outcome is MeasurementOutcome.REALIZED
        assert measurement.sequence == 1
        assert len(measurement.evidence_sha256) == 64
        cost, co2e = measurement.estimates
        assert cost.quantity == "cost" and cost.outcome is QuantityOutcome.REALIZED and cost.basis == "total"
        assert cost.annualized == pytest.approx(0.024 * HOURS_PER_YEAR)
        assert co2e.outcome is QuantityOutcome.NOT_CLAIMED and co2e.claimed_annualized == 0.0
        gates = {gate.gate: gate.status.value for gate in measurement.health}
        assert gates["cpu_p95"] == "passed"
        assert gates["restarts"] == "passed"
        # No health collector: the signals are reported, not silently passed (C0.9).
        assert gates["oom_kills"] == gates["readiness"] == gates["cpu_throttling"] == "not_available"
        baseline = baseline_of(record)
        assert baseline["latest_measurement_id"] == measurement.measurement_id
        assert baseline["measurement_ids"] == [measurement.measurement_id]
        assert baseline["measured_ledger_input"]["measurement_id"] == measurement.measurement_id
        assert baseline["measured_ledger_input"]["cost_outcome"] == "realized"
        event = (await repo.get_events(record.id))[-1]
        payload = event.payload.model_dump()
        assert event.event_type == "verified"
        assert payload["measurement_id"] == measurement.measurement_id
        assert {gate["gate"] for gate in payload["health"]} >= {"oom_kills", "readiness", "cpu_throttling"}

    async def test_measurement_rows_are_immutable(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        metrics = WindowedMetricsRepo(rows(applied_at - WINDOW, applied_at) + rows(applied_at, now, cost=0.008))
        record = (await _verifier(repo, metrics).verify_due())[0]
        measurement = (await measurements.list_for_recommendation(record.id))[0]

        async with db_manager.connection_scope() as conn:
            with pytest.raises(Exception, match="immutable"):
                await conn.execute("UPDATE savings_measurements SET outcome = 'not_realized'")
            await conn.rollback()
            with pytest.raises(Exception, match="immutable"):
                await conn.execute("DELETE FROM savings_measurements")
            await conn.rollback()

        assert (await measurements.get(measurement.measurement_id)) == measurement

    async def test_a_decided_cycle_is_replayed_not_re_decided(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        metrics = WindowedMetricsRepo(rows(applied_at - WINDOW, applied_at) + rows(applied_at, now, cost=0.008))
        record = (await _verifier(repo, metrics).verify_due())[0]
        first = (await measurements.list_for_recommendation(record.id))[0]

        replay = first.model_copy(update={"measurement_id": "other", "outcome": MeasurementOutcome.NOT_REALIZED})
        stored = await measurements.insert(replay)

        assert stored == first
        assert len(await measurements.list_for_recommendation(record.id)) == 1


class TestFixedHorizon:
    async def test_windows_are_fixed_and_do_not_overlap_the_rollout(self, repo, measurements):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        applied_at = now - timedelta(hours=30)  # verified late: the window must not grow
        merged_at = applied_at - timedelta(hours=1)
        await repo.save_recommendations(
            [_record(applied_at, baseline={"restart_count": 0, "baseline_window_end": merged_at.isoformat()})]
        )
        metrics = WindowedMetricsRepo(
            rows(merged_at - WINDOW, merged_at, cost=0.01)
            # Rollout in progress between merge and completion: excluded from both windows.
            + rows(merged_at, applied_at, cost=0.05)
            + rows(applied_at, applied_at + WINDOW, cost=0.008)
            # After the fixed horizon: excluded.
            + rows(applied_at + WINDOW, now, cost=0.001)
        )

        record = (await _verifier(repo, metrics).verify_due())[0]

        assert record.status is RecommendationStatus.VERIFIED
        assert record.measured_cost_saved == pytest.approx(0.024 * HOURS_PER_YEAR)
        assert record.verification_window_start == applied_at
        assert record.verification_window_end == applied_at + WINDOW
        windows = (await measurements.list_for_recommendation(record.id))[0].windows
        assert windows["baseline"] == {"start": (merged_at - WINDOW).isoformat(), "end": merged_at.isoformat()}
        assert windows["after"] == {"start": applied_at.isoformat(), "end": (applied_at + WINDOW).isoformat()}

    async def test_window_not_elapsed_is_not_measured(self, repo):
        now = datetime.now(timezone.utc)
        await repo.save_recommendations([_record(now - timedelta(hours=6, minutes=10))])
        metrics = WindowedMetricsRepo(rows(now - timedelta(hours=13), now))

        # 6h window + 0.6h grace has not elapsed yet.
        assert await _verifier(repo, metrics).verify_due() == []

    async def test_traffic_is_recorded_but_not_used_to_normalize(self, repo):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, applied_at, cost=0.01, network_receive_bytes=500.0, network_transmit_bytes=500.0)
            + rows(applied_at, now, cost=0.008, network_receive_bytes=1000.0, network_transmit_bytes=1000.0)
        )

        record = (await _verifier(repo, metrics).verify_due())[0]

        assert record.measured_cost_saved == pytest.approx(0.024 * HOURS_PER_YEAR)
        assert baseline_of(record)["measured_ledger_input"]["traffic_ratio"] == pytest.approx(2.0)


class TestProvenance:
    async def test_cost_source_none_is_not_trusted(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, now + timedelta(hours=8), model=ProvenancedMetric, cost_source="none")
        )

        first = (await _verifier(repo, metrics).verify_due())[0]
        assert first.status is RecommendationStatus.VERIFYING  # extended once for coverage
        second = (await _verifier(repo, metrics, clock=lambda: now + timedelta(hours=7)).verify_due())[0]

        assert second.status is RecommendationStatus.APPLIED
        assert second.verification_status == "inconclusive"
        stored = await measurements.list_for_recommendation(second.id)
        assert [item.outcome for item in stored] == [MeasurementOutcome.INCONCLUSIVE]
        # The measured input is kept, never deleted on inconclusive (C0.13).
        assert baseline_of(second)["measured_ledger_input"]["outcome"] == "inconclusive"
        assert (await repo.get_events(second.id))[-1].event_type == "inconclusive"

    async def test_trusted_container_rows_are_measured_at_container_scope(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at, container_name="app")])
        container = rows(
            applied_at - WINDOW,
            applied_at,
            container="app",
            cost=0.006,
            model=ProvenancedMetric,
            cost_source="opencost",
            usage_source="prometheus",
        ) + rows(
            applied_at,
            now,
            container="app",
            cost=0.004,
            model=ProvenancedMetric,
            cost_source="opencost",
            usage_source="prometheus",
        )
        for row in container:
            row.is_estimated = True  # provenance, not the legacy flag, decides trust
        pods = rows(applied_at - WINDOW, now, cost=0.02)
        metrics = WindowedMetricsRepo(pods, container)

        record = (await _verifier(repo, metrics).verify_due())[0]

        assert record.status is RecommendationStatus.VERIFIED
        assert record.measured_cost_saved == pytest.approx(0.024 * HOURS_PER_YEAR)
        measurement = (await measurements.list_for_recommendation(record.id))[0]
        assert measurement.scope == "container"

    async def test_untrusted_container_rows_fall_back_to_the_workload(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at, container_name="app")])
        container = rows(applied_at - WINDOW, now, container="app", cost=0.006)
        for row in container:
            row.is_estimated = True  # legacy blanket flag (BUG-29)
        pods = rows(applied_at - WINDOW, applied_at, cost=0.01) + rows(applied_at, now, cost=0.008)
        metrics = WindowedMetricsRepo(pods, container)

        record = (await _verifier(repo, metrics).verify_due())[0]

        assert record.status is RecommendationStatus.VERIFIED
        assert record.measured_cost_saved == pytest.approx(0.024 * HOURS_PER_YEAR)
        assert (await measurements.list_for_recommendation(record.id))[0].scope == "workload_fallback"

    @pytest.mark.parametrize("total_cost", [-0.01, float("nan"), float("inf")])
    def test_negative_or_non_finite_cost_is_not_trusted(self, monkeypatch, total_cost):
        monkeypatch.setattr(verifier_module, "_provenance_module", None)
        row = ProvenancedMetric(pod_name="api-a", namespace="prod", total_cost=total_cost, cost_source="opencost")

        assert verifier_module.cost_is_trusted(row) is False

    def test_rows_follow_the_a11_provenance_helper_when_installed(self, monkeypatch):
        seen: list[tuple[str, str | None]] = []

        def record(kind, verdict):
            def check(row):
                seen.append((kind, getattr(row, f"{kind}_source", None)))
                return verdict

            return check

        helper = SimpleNamespace(
            is_cost_trusted=record("cost", True),
            is_usage_trusted=record("usage", False),
            is_intensity_measured=record("intensity", True),
            is_power_profiled=record("power", False),
        )
        monkeypatch.setattr(verifier_module, "_provenance_module", helper)
        tagged = ProvenancedMetric(
            pod_name="api-a",
            namespace="prod",
            total_cost=0.01,
            cost_source="custom_rate",
            usage_source="prometheus",
            intensity_source="realtime",
            power_source="default_profile",
        )
        legacy = ProvenancedMetric(pod_name="api-a", namespace="prod", total_cost=0.01, is_estimated=False)

        # Tagged rows: the helper decides, whatever the verifier's own defaults say.
        assert verifier_module.cost_is_trusted(tagged) is True
        assert verifier_module.usage_is_trusted(tagged) is False
        assert verifier_module.carbon_is_trusted(tagged) is False
        assert seen == [
            ("cost", "custom_rate"),
            ("usage", "prometheus"),
            ("power", "default_profile"),
        ]
        # Rows written before provenance existed keep the legacy rule (C0.3 fallback).
        seen.clear()
        assert verifier_module.cost_is_trusted(legacy) is True
        assert verifier_module.usage_is_trusted(legacy) is True
        assert seen == []


class TestAttributionGroups:
    async def _pair(self, repo, applied_at, *, plans=("plan-1", "plan-1"), container="app"):
        await repo.save_recommendations(
            [
                _record(
                    applied_at,
                    container_name=container,
                    potential_savings_cost=300.0,
                    baseline={"restart_count": 0, "plan_id": plans[0]},
                ),
                _record(
                    applied_at,
                    container_name=container,
                    type=RecommendationType.RIGHTSIZING_MEMORY,
                    potential_savings_cost=100.0,
                    baseline={"restart_count": 0, "plan_id": plans[1]},
                ),
            ]
        )

    async def test_cpu_and_memory_of_one_plan_split_the_cost_components(self, repo, measurements):
        now, applied_at = _timeline()
        await self._pair(repo, applied_at)
        provenance = dict(model=ProvenancedMetric, cost_source="opencost", container="app", memory=200 * 1024**2)
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, now),
            rows(applied_at - WINDOW, applied_at, cost=0.01, cost_cpu=0.006, cost_ram=0.004, **provenance)
            + rows(applied_at, now, cost=0.007, cost_cpu=0.004, cost_ram=0.003, **provenance),
        )

        updated = await _verifier(repo, metrics).verify_due()

        cpu, memory = sorted(updated, key=lambda record: record.type.value)
        assert cpu.type is RecommendationType.RIGHTSIZING_CPU
        assert cpu.measured_cost_saved == pytest.approx(0.002 * 12 * HOURS_PER_YEAR)
        assert memory.measured_cost_saved == pytest.approx(0.001 * 12 * HOURS_PER_YEAR)
        # Together they claim the workload's cost delta once, not twice (BUG-36).
        assert cpu.measured_cost_saved is not None and memory.measured_cost_saved is not None
        assert cpu.measured_cost_saved + memory.measured_cost_saved == pytest.approx(0.003 * 12 * HOURS_PER_YEAR)
        cpu_measurement = (await measurements.list_for_recommendation(cpu.id))[0]
        memory_measurement = (await measurements.list_for_recommendation(memory.id))[0]
        assert cpu_measurement.group_id == memory_measurement.group_id
        assert cpu_measurement.group_recommendation_ids == sorted([rec_id(cpu), rec_id(memory)])
        assert cpu_measurement.estimates[0].basis == "cost_cpu"
        assert memory_measurement.estimates[0].basis == "cost_ram"

    async def test_without_components_the_delta_is_split_by_projected_share(self, repo):
        now, applied_at = _timeline()
        await self._pair(repo, applied_at, container=None)
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, applied_at, cost=0.01, memory=200 * 1024**2)
            + rows(applied_at, now, cost=0.007, memory=200 * 1024**2)
        )

        updated = await _verifier(repo, metrics).verify_due()

        cpu, memory = sorted(updated, key=lambda record: record.type.value)
        total = 0.003 * 12 * HOURS_PER_YEAR
        assert cpu.measured_cost_saved == pytest.approx(total * 0.75)
        assert memory.measured_cost_saved == pytest.approx(total * 0.25)

    def test_all_zero_components_do_not_hide_a_real_cost(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        provenance = dict(model=ProvenancedMetric, cost_source="opencost")
        zero = rows(start, start + timedelta(hours=1), cost=0.01, cost_cpu=0.0, cost_ram=0.0, **provenance)
        split = rows(start, start + timedelta(hours=1), cost=0.01, cost_cpu=0.006, cost_ram=0.004, **provenance)

        assert window_rates(zero, 300).cost_cpu_per_hour is None
        assert window_rates(zero, 300).cost_per_hour == pytest.approx(0.12)
        assert window_rates(split, 300).cost_cpu_per_hour == pytest.approx(0.072)

    async def test_records_of_different_plans_are_measured_alone(self, repo):
        now, applied_at = _timeline()
        await self._pair(repo, applied_at, plans=("plan-1", "plan-2"), container=None)
        records = await _all(repo)

        assert [len(group) for group in attribution_groups(records)] == [1, 1]

    async def test_a_failed_gate_sends_the_whole_change_to_rollback_review(self, repo):
        now, applied_at = _timeline()
        await self._pair(repo, applied_at, container=None)
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, applied_at, cost=0.01, memory=200 * 1024**2)
            # Memory p95 600Mi exceeds the 512Mi request x 1.1.
            + rows(applied_at, now, cost=0.007, memory=600 * 1024**2)
        )

        updated = await _verifier(repo, metrics).verify_due()

        assert {record.status for record in updated} == {RecommendationStatus.ROLLBACK_REVIEW}


class TestOutcomes:
    async def test_cost_increase_is_not_realized_and_reported_signed(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, applied_at, cost=0.01) + rows(applied_at, now, cost=0.012)
        )

        record = (await _verifier(repo, metrics).verify_due())[0]

        assert record.status is RecommendationStatus.APPLIED
        assert record.verification_status == "failed"
        assert record.measured_cost_saved == 0.0
        cost = (await measurements.list_for_recommendation(record.id))[0].estimates[0]
        assert cost.outcome is QuantityOutcome.NOT_REALIZED
        assert cost.annualized == pytest.approx(-0.024 * HOURS_PER_YEAR)
        assert (await repo.get_events(record.id))[-1].event_type == "not_realized"

    async def test_projection_is_reported_not_gated(self, repo, measurements):
        now, applied_at = _timeline()
        # Projection 10x the real saving: still realized, accuracy reported.
        await repo.save_recommendations([_record(applied_at, potential_savings_cost=2100.0)])
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, applied_at, cost=0.01) + rows(applied_at, now, cost=0.008)
        )

        record = (await _verifier(repo, metrics).verify_due())[0]

        assert record.status is RecommendationStatus.VERIFIED
        measurement = (await measurements.list_for_recommendation(record.id))[0]
        assert measurement.estimates[0].projection_accuracy == pytest.approx(0.024 * HOURS_PER_YEAR / 2100.0)
        assert any("not a gate" in reason for reason in measurement.reasons)

    async def test_remeasure_creates_a_superseding_measurement(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, now, cost=0.01) + rows(now, now + timedelta(hours=8), cost=0.008)
        )
        first = (await _verifier(repo, metrics).verify_due())[0]
        assert first.verification_status == "failed"

        # Terminal: nothing happens until a re-measurement is requested (BUG-31).
        later = _verifier(repo, metrics, clock=lambda: now + timedelta(hours=8))
        assert await later.verify_due() == []

        await RecommendationLifecycle(repo).request_remeasure(rec_id(first), reason="retry", window_hours=6)
        remeasured = (await later.verify_due())[0]

        assert remeasured.status is RecommendationStatus.VERIFIED
        assert remeasured.measured_cost_saved == pytest.approx(0.024 * HOURS_PER_YEAR)
        history = await measurements.list_for_recommendation(first.id)
        assert [item.outcome for item in history] == [MeasurementOutcome.NOT_REALIZED, MeasurementOutcome.REALIZED]
        assert history[1].sequence == 2
        assert history[1].supersedes_measurement_id == history[0].measurement_id
        # The counterfactual still ends at the change.
        assert history[1].windows["baseline"]["end"] == applied_at.isoformat()


class TestPassHardening:
    async def test_a_decision_persisted_before_a_crash_is_replayed_not_re_observed(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        record_id = rec_id((await _all(repo))[0])
        after = {"start": applied_at.isoformat(), "end": (applied_at + WINDOW).isoformat()}
        decided = SavingsMeasurement(
            measurement_id="decided-before-crash",
            recommendation_id=record_id,
            group_id="group-1",
            group_recommendation_ids=[record_id],
            windows={"baseline": {"start": (applied_at - WINDOW).isoformat(), "end": after["start"]}, "after": after},
            outcome=MeasurementOutcome.NOT_REALIZED,
            reasons=["measured cost change is -1.00/yr (no saving)"],
        ).with_evidence_digest()
        await measurements.insert(decided)

        # No metrics at all: observing again would extend the window instead of replaying.
        updated = await _verifier(repo, WindowedMetricsRepo()).verify_due()

        assert [(record.status, record.verification_status) for record in updated] == [
            (RecommendationStatus.APPLIED, "failed")
        ]
        assert baseline_of(updated[0])["latest_measurement_id"] == "decided-before-crash"
        assert [item.measurement_id for item in await measurements.list_for_recommendation(record_id)] == [
            "decided-before-crash"
        ]
        events = [event.event_type for event in await repo.get_events(record_id)]
        assert "verification_started" not in events
        assert events[-1] == "not_realized"

    async def test_outcome_metric_is_counted_once_the_transition_is_written(self, repo, monkeypatch):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, applied_at, cost=0.01) + rows(applied_at, now, cost=0.008)
        )
        counted: list[str] = []
        monkeypatch.setattr(
            "greenkube.core.observability.record_verification",
            lambda outcome, failed_gates: counted.append(outcome),
        )
        verifier = _verifier(repo, metrics)
        mark_verified = verifier.lifecycle.mark_verified

        async def crash(*args, **kwargs):
            raise RuntimeError("database went away")

        monkeypatch.setattr(verifier.lifecycle, "mark_verified", crash)
        assert await verifier.verify_due() == []
        assert counted == []

        monkeypatch.setattr(verifier.lifecycle, "mark_verified", mark_verified)
        updated = await verifier.verify_due()

        assert [record.status for record in updated] == [RecommendationStatus.VERIFIED]
        assert counted == ["verified"]

    async def test_groups_measured_over_one_window_share_their_reads(self, repo):
        now, applied_at = _timeline()
        await repo.save_recommendations(
            [_record(applied_at, container_name="app"), _record(applied_at, container_name="sidecar")]
        )
        pods = rows(applied_at - WINDOW, applied_at, cost=0.02) + rows(applied_at, now, cost=0.016)
        containers = [
            row
            for name in ("app", "sidecar")
            for row in rows(applied_at - WINDOW, applied_at, cost=0.01, container=name)
            + rows(applied_at, now, cost=0.008, container=name)
        ]
        metrics = WindowedMetricsRepo(pods, containers)

        updated = await _verifier(repo, metrics).verify_due()

        assert [record.status for record in updated] == [RecommendationStatus.VERIFIED] * 2
        # Two container-scoped groups, one window: container and pod rows are read once per window.
        assert len(metrics.reads) == len(set(metrics.reads)) == 4


def _noisy(metrics, seed: int, spread: float):
    import random

    rng = random.Random(seed)
    return [
        row.model_copy(update={"total_cost": row.total_cost * (1 + rng.uniform(-spread, spread))}) for row in metrics
    ]


class TestIntervalDecision:
    """The interim verifier decides on an interval, never on a point estimate (MEA-16)."""

    async def test_a_saving_inside_the_noise_is_inconclusive_not_realized(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        # A 5% point saving (above the old 2% rule) under +-40% sample noise.
        metrics = WindowedMetricsRepo(
            _noisy(rows(applied_at - WINDOW, applied_at, cost=0.01), 1, 0.4)
            + _noisy(rows(applied_at, now, cost=0.0095), 2, 0.4)
        )

        record = (await _verifier(repo, metrics).verify_due())[0]

        assert record.status is not RecommendationStatus.VERIFIED
        measurement = (await measurements.list_for_recommendation(record.id))[0]
        assert measurement.outcome is MeasurementOutcome.INCONCLUSIVE
        cost = measurement.estimates[0]
        assert cost.outcome is QuantityOutcome.INCONCLUSIVE
        floor = 0.02 * (cost.before_per_hour or 0.0)
        assert cost.ci_low_per_hour is not None and cost.ci_high_per_hour is not None
        assert cost.ci_low_per_hour <= floor < cost.ci_high_per_hour
        assert measurement.windows["method"] == "interim_before_after"
        assert any("straddles the noise floor" in reason for reason in measurement.reasons)

    async def test_a_deterministic_saving_is_still_realized_with_a_zero_width_interval(self, repo, measurements):
        now, applied_at = _timeline()
        await repo.save_recommendations([_record(applied_at)])
        metrics = WindowedMetricsRepo(
            rows(applied_at - WINDOW, applied_at, cost=0.01) + rows(applied_at, now, cost=0.008)
        )

        record = (await _verifier(repo, metrics).verify_due())[0]

        assert record.status is RecommendationStatus.VERIFIED
        cost = (await measurements.list_for_recommendation(record.id))[0].estimates[0]
        assert cost.ci_low_per_hour == pytest.approx(0.024) and cost.ci_high_per_hour == pytest.approx(0.024)

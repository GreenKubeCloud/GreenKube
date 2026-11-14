"""Reliability outcome of request increases: the OOM-kill rate drop (roadmap H1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from greenkube.models.verification import HealthGateResult, HealthGateStatus, WorkloadHealthObservation
from greenkube.verify.model import ChangeOutcome
from greenkube.verify.strategy import (
    container_reliability,
    increase_reliability,
    is_reliability_increase,
    oom_rate_outcome,
    raises_memory_request,
)

WEEK = 7 * 86400.0
MIB = 1024**2


def _member(*, current=512 * MIB, proposed=768 * MIB, oom_events=6, window=WEEK, container="api"):
    return SimpleNamespace(
        type="RIGHTSIZING_MEMORY",
        container_name=container,
        current_cpu_request_millicores=None,
        recommended_cpu_request_millicores=None,
        current_memory_request_bytes=current,
        recommended_memory_request_bytes=proposed,
        evidence=SimpleNamespace(oom_events=oom_events, observation_window_seconds=window),
    )


def _cpu_member(*, current=250, proposed=500, oom_events=6, window=WEEK, container="api"):
    """A CPU-request recommendation: its evidence shares the container's OOM count."""
    return SimpleNamespace(
        type="RIGHTSIZING_CPU",
        container_name=container,
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=proposed,
        current_memory_request_bytes=512 * MIB,
        recommended_memory_request_bytes=512 * MIB,
        evidence=SimpleNamespace(oom_events=oom_events, observation_window_seconds=window),
    )


def _gates(oom: int | None, restarts: int | None = None, *, max_restarts: int = 0) -> list[HealthGateResult]:
    """Gates as the verifier reads them: every OOM kill also restarts its container (restarts >= kills)."""
    restart_delta = (oom or 0) if restarts is None else restarts
    gates = [
        HealthGateResult(
            gate="restarts",
            status=HealthGateStatus.FAILED if restart_delta > max_restarts else HealthGateStatus.PASSED,
            value=restart_delta,
            threshold=max_restarts,
        )
    ]
    if oom is None:
        gates.append(HealthGateResult(gate="oom_kills", status=HealthGateStatus.NOT_AVAILABLE, detail="no source"))
    else:
        status = HealthGateStatus.FAILED if oom > 0 else HealthGateStatus.PASSED
        gates.append(HealthGateResult(gate="oom_kills", status=status, value=oom, threshold=0))
    return gates


def _gate(gates: list[HealthGateResult], name: str) -> HealthGateResult:
    (gate,) = [gate for gate in gates if gate.gate == name]
    return gate


@pytest.mark.parametrize(
    ("before", "after", "outcome"),
    [
        (6, 0, ChangeOutcome.REALIZED),  # p = 0.5^6
        (2, 0, ChangeOutcome.INCONCLUSIVE),  # lower, too few kills to tell
        (3, 4, ChangeOutcome.NOT_REALIZED),
        (3, 3, ChangeOutcome.NOT_REALIZED),
        (0, 0, ChangeOutcome.INCONCLUSIVE),  # no OOM history: judged on headroom
        (None, 0, ChangeOutcome.INCONCLUSIVE),
        (4, None, ChangeOutcome.INCONCLUSIVE),
    ],
)
def test_oom_rate_outcome(before, after, outcome) -> None:
    result = oom_rate_outcome(before, WEEK, after, WEEK)
    assert result.outcome is outcome
    assert result.reason


def test_the_rate_accounts_for_unequal_windows() -> None:
    """Six kills in two weeks then one in a week is no drop to speak of; zero in a week after 12 is."""
    assert oom_rate_outcome(6, 2 * WEEK, 3, WEEK).outcome is ChangeOutcome.NOT_REALIZED
    realized = oom_rate_outcome(12, 2 * WEEK, 0, WEEK)
    assert realized.outcome is ChangeOutcome.REALIZED
    assert realized.baseline_rate_per_day == pytest.approx(12 / 14)
    assert realized.post_rate_per_day == 0
    assert realized.as_dict()["outcome"] == "realized"


def test_an_oom_increase_passes_its_gates_when_the_rate_drops() -> None:
    """Regression (H1): one OOM kill after an OOM-driven increase sent it to rollback review, which would
    re-introduce the kills. The rate comparison replaces the "any kill fails" gate, and the restart that
    kill caused no longer fails the restart gate (VERIFICATION_MAX_RESTART_DELTA=0)."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates, outcome = container_reliability(
        [_member(oom_events=12)], _gates(1), start, start + timedelta(days=7), oom_restarts=1
    )
    assert _gate(gates, "oom_kills").status is HealthGateStatus.PASSED
    assert _gate(gates, "oom_kills").value == 1 and _gate(gates, "oom_kills").threshold == 12
    restarts = _gate(gates, "restarts")
    assert restarts.status is HealthGateStatus.PASSED and restarts.value == 0
    assert "1 OOM kill" in restarts.detail
    assert outcome is not None and outcome.outcome is ChangeOutcome.REALIZED
    assert [gate.gate for gate in gates] == ["restarts", "oom_kills"]


def test_restarts_other_than_oom_kills_still_fail() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates, _ = container_reliability(
        [_member(oom_events=12)], _gates(1, restarts=3), start, start + timedelta(days=7), oom_restarts=1
    )
    assert _gate(gates, "oom_kills").status is HealthGateStatus.PASSED
    restarts = _gate(gates, "restarts")
    assert restarts.status is HealthGateStatus.FAILED and restarts.value == 2


def test_one_pod_crash_looping_on_oom_is_not_a_drop() -> None:
    """Review (H1): the post count is in the unit of the baseline (OOM restarts), never pods with a kill.

    One pod OOM-killed 20 times in a week after the increase reads as 1 on the pod-count gate; against
    6 kills a week before, that is no drop."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates, outcome = container_reliability(
        [_member(oom_events=6)], _gates(1, restarts=20), start, start + timedelta(days=7), oom_restarts=20
    )
    assert outcome is not None and outcome.outcome is ChangeOutcome.NOT_REALIZED
    assert outcome.post_events == 20
    assert _gate(gates, "oom_kills").status is HealthGateStatus.FAILED


def test_an_unknown_oom_restart_count_keeps_the_strict_gates() -> None:
    """Without the OOM restart count (no kube-state-metrics query, Kubernetes API fallback) nothing is relaxed."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates = _gates(1)
    kept, outcome = container_reliability([_member(oom_events=12)], gates, start, start + timedelta(days=7))
    assert kept == gates
    assert outcome is not None and outcome.outcome is ChangeOutcome.INCONCLUSIVE


def test_an_increase_that_did_not_help_fails_its_gate() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates, outcome = container_reliability(
        [_member(oom_events=2)], _gates(5), start, start + timedelta(days=7), oom_restarts=5
    )
    assert _gate(gates, "oom_kills").status is HealthGateStatus.FAILED
    assert outcome is not None and outcome.outcome is ChangeOutcome.NOT_REALIZED


def test_decreases_and_increases_without_oom_history_keep_their_gates() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    end = start + timedelta(days=7)
    decrease = _member(current=768 * MIB, proposed=512 * MIB)
    assert not is_reliability_increase(decrease)
    gates = _gates(1)
    assert container_reliability([decrease], gates, start, end, oom_restarts=1) == (gates, None)

    kept, outcome = container_reliability([_member(oom_events=0)], gates, start, end, oom_restarts=1)
    assert kept == gates
    assert outcome is not None and outcome.outcome is ChangeOutcome.INCONCLUSIVE


def test_unknown_post_window_kills_keep_the_gate_unavailable() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates = _gates(None)
    kept, outcome = container_reliability([_member()], gates, start, start + timedelta(days=7), oom_restarts=None)
    assert kept == gates
    assert outcome is not None and outcome.outcome is ChangeOutcome.INCONCLUSIVE


class _Verifier:
    """The verifier's per-container health check: gates plus the OOM restarts it read."""

    def __init__(self, gates_for, oom_restarts_for) -> None:
        self.gates_for = gates_for
        self.oom_restarts_for = oom_restarts_for

    async def health_check_for(self, group, start, end, *, strict=False):
        container = group[0].container_name
        health = WorkloadHealthObservation(
            oom_kill_count=1, oom_restart_count=self.oom_restarts_for(container), readiness_ratio=1.0
        )
        return self.gates_for(container), health


def _service(verifier: _Verifier):
    from unittest.mock import MagicMock

    from greenkube.verify.service import ChangeMeasurementService

    return ChangeMeasurementService(
        MagicMock(), MagicMock(), MagicMock(), verifier=cast(Any, verifier), config=MagicMock()
    )


@pytest.mark.asyncio
async def test_the_measurement_service_judges_increase_gates_on_the_rate() -> None:
    """The change-measurement health check (interim and final) applies the rate comparison, on the OOM
    restarts the health collector read, and leaves those restarts out of the restart gate."""
    service = _service(_Verifier(lambda _: _gates(1), lambda _: 1))
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates = await service._health(cast(Any, [_member(oom_events=12)]), start, start + timedelta(days=7))
    assert [gate.status for gate in gates] == [HealthGateStatus.PASSED, HealthGateStatus.PASSED]

    decrease = await service._health(
        cast(Any, [_member(current=768 * MIB, proposed=512 * MIB)]), start, start + timedelta(days=7)
    )
    assert [gate.gate for gate in decrease if gate.status is HealthGateStatus.FAILED] == ["restarts", "oom_kills"]


@pytest.mark.asyncio
async def test_the_measurement_service_keeps_the_strict_gates_without_an_oom_restart_count() -> None:
    service = _service(_Verifier(lambda _: _gates(1), lambda _: None))
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates, outcome = await service._health_and_reliability(
        cast(Any, [_member(oom_events=12)]), start, start + timedelta(days=7)
    )
    assert [gate.gate for gate in gates if gate.status is HealthGateStatus.FAILED] == ["restarts", "oom_kills"]
    assert outcome is not None and outcome.outcome is ChangeOutcome.INCONCLUSIVE


def test_a_decreased_container_keeps_its_strict_gate_in_a_mixed_plan() -> None:
    """Regression (review): one container's OOM-driven memory increase must not hide the OOM kills of
    another container whose memory was lowered in the same change."""
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = start + timedelta(days=1)
    increase = _member(container="a", oom_events=20)
    decrease = _member(container="b", current=1024 * MIB, proposed=256 * MIB, oom_events=0)
    # Restart counters are per pod: both containers see the 2 restarts of b's OOM kills.
    judged, outcome = increase_reliability(
        [increase, decrease], {"a": _gates(0, restarts=2), "b": _gates(2)}, start, end, {"a": 0, "b": 2}
    )
    assert _gate(judged["b"], "oom_kills").status is HealthGateStatus.FAILED
    assert _gate(judged["b"], "oom_kills").value == 2
    assert _gate(judged["b"], "restarts").status is HealthGateStatus.FAILED
    assert _gate(judged["a"], "oom_kills").status is HealthGateStatus.PASSED
    assert _gate(judged["a"], "restarts").status is HealthGateStatus.FAILED
    # The change-level outcome only counts the increased container: 20 kills a day, none since.
    assert outcome is not None and outcome.baseline_events == 20 and outcome.post_events == 0


def test_oom_restarts_of_the_increased_container_leave_every_restart_gate() -> None:
    """Pod-level restart counters: the OOM restarts judged by a's rate gate leave b's restart gate too."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    end = start + timedelta(days=7)
    increase = _member(container="a", oom_events=30)
    decrease = _member(container="b", current=1024 * MIB, proposed=512 * MIB, oom_events=0)
    judged, _ = increase_reliability(
        [increase, decrease], {"a": _gates(1, restarts=2), "b": _gates(0, restarts=2)}, start, end, {"a": 2, "b": 0}
    )
    for container in ("a", "b"):
        assert _gate(judged[container], "restarts").status is HealthGateStatus.PASSED
    assert _gate(judged["a"], "oom_kills").status is HealthGateStatus.PASSED
    assert _gate(judged["b"], "oom_kills").status is HealthGateStatus.PASSED


def test_a_cpu_only_increase_keeps_the_any_kill_gate() -> None:
    """A CPU increase cannot cure OOM kills: kills after it still fail the gates, whatever the rate."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    end = start + timedelta(days=7)
    cpu = _cpu_member(oom_events=20)
    assert is_reliability_increase(cpu) and not raises_memory_request(cpu)
    gates, outcome = container_reliability([cpu], _gates(1), start, end, oom_restarts=1)
    assert _gate(gates, "oom_kills").status is HealthGateStatus.FAILED
    assert _gate(gates, "restarts").status is HealthGateStatus.FAILED
    assert outcome is None
    judged, change_outcome = increase_reliability([cpu], {"api": _gates(1)}, start, end, {"api": 1})
    assert _gate(judged["api"], "oom_kills").status is HealthGateStatus.FAILED
    assert _gate(judged["api"], "restarts").status is HealthGateStatus.FAILED
    assert change_outcome is None


def test_a_cpu_and_memory_increase_on_one_container_is_judged_on_the_rate() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    end = start + timedelta(days=7)
    members = [_cpu_member(oom_events=12), _member(oom_events=12)]
    gates, outcome = container_reliability(members, _gates(1), start, end, oom_restarts=1)
    assert _gate(gates, "oom_kills").status is HealthGateStatus.PASSED
    assert _gate(gates, "oom_kills").threshold == 12
    assert _gate(gates, "restarts").status is HealthGateStatus.PASSED
    assert outcome is not None and outcome.outcome is ChangeOutcome.REALIZED


def test_a_cpu_increase_with_a_memory_decrease_keeps_the_strict_gate() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    members = [_cpu_member(oom_events=12), _member(current=768 * MIB, proposed=512 * MIB, oom_events=12)]
    gates, outcome = container_reliability(members, _gates(1), start, start + timedelta(days=7), oom_restarts=1)
    assert _gate(gates, "oom_kills").status is HealthGateStatus.FAILED
    assert _gate(gates, "restarts").status is HealthGateStatus.FAILED
    assert outcome is None


@pytest.mark.asyncio
async def test_the_measurement_service_keeps_decrease_gates_strict_in_a_mixed_change() -> None:
    def gates_for(container):
        # b's two OOM kills restart the pod: both containers read the pod-level restart delta.
        return _gates(2 if container == "b" else 0, restarts=2)

    service = _service(_Verifier(gates_for, lambda container: 2 if container == "b" else 0))
    start = datetime.now(timezone.utc) - timedelta(days=1)
    members = [
        _member(container="a", oom_events=20),
        _member(container="b", current=1024 * MIB, proposed=256 * MIB, oom_events=0),
    ]
    gates, outcome = await service._health_and_reliability(cast(Any, members), start, start + timedelta(days=1))
    failed = [gate for gate in gates if gate.status is HealthGateStatus.FAILED]
    assert any(gate.gate == "oom_kills" and gate.detail.startswith("[b]") for gate in failed)
    assert not any(gate.gate == "oom_kills" and gate.detail.startswith("[a]") for gate in failed)
    assert outcome is not None and outcome.post_events == 0


def _limited_member(*, limit: int, raised_to: int | None, oom_events: int = 12):
    """A memory increase whose evidence knows the container's memory limit and its planned limit edit."""
    member = _member(current=24 * MIB, proposed=48 * MIB, oom_events=oom_events)
    member.evidence.current = SimpleNamespace(memory_limit_bytes=limit)
    member.evidence.limit_changes = (
        []
        if raised_to is None
        else [SimpleNamespace(resource="memory", action="set", current=limit, proposed=raised_to)]
    )
    return member


def test_an_oom_increase_that_raised_the_limit_is_judged_on_the_rate() -> None:
    """e2e (request = limit = 24Mi): the fix raises the limit above the new request; the rate judges it."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates, outcome = container_reliability(
        [_limited_member(limit=24 * MIB, raised_to=53 * MIB)], _gates(1), start, start + timedelta(days=7), 1
    )
    assert _gate(gates, "oom_kills").status is HealthGateStatus.PASSED
    assert outcome is not None and outcome.outcome is ChangeOutcome.REALIZED


@pytest.mark.parametrize("raised_to", [None, 24 * MIB, 16 * MIB])
def test_an_oom_increase_that_kept_or_lowered_the_limit_keeps_the_strict_gate(raised_to: int | None) -> None:
    """The kernel kills at the limit: a request increase that left it in place cannot cure the kills."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates = _gates(1)
    kept, _ = container_reliability(
        [_limited_member(limit=24 * MIB, raised_to=raised_to)], gates, start, start + timedelta(days=7), 1
    )
    assert kept == gates
    assert _gate(kept, "oom_kills").status is HealthGateStatus.FAILED


# --------------------------------------------------------------------------- #
# Baseline exposure of young workloads                                        #
# --------------------------------------------------------------------------- #

#: Scan time of the live e2e: the cluster's data started 8.4 hours before it.
SCAN = datetime(2026, 11, 10, 18, 0, tzinfo=timezone.utc)
DATA_WINDOW = 8.4 * 3600


def _young_member(*, first_observed: datetime | None, oom_events: int = 23, container: str = "api"):
    """A memory increase whose evidence window starts at the cluster's data start, not at the workload's."""
    member = _member(oom_events=oom_events, window=DATA_WINDOW, container=container)
    member.evidence.observation_window_start = SCAN - timedelta(seconds=DATA_WINDOW)
    member.evidence.observation_window_end = SCAN
    member.evidence.first_observed_at = first_observed
    return member


def test_a_young_workload_baseline_counts_only_the_time_it_was_observed() -> None:
    """Regression (live e2e): a workload 40 minutes old was treated as observed for the 8.4-hour data
    window, so its baseline OOM rate (23 kills) was about 12 times too low."""
    post_start = SCAN + timedelta(hours=1)
    _, outcome = container_reliability(
        [_young_member(first_observed=SCAN - timedelta(minutes=40))],
        _gates(0),
        post_start,
        post_start + timedelta(hours=1.2),
        oom_restarts=0,
    )
    assert outcome is not None
    assert outcome.baseline_hours == pytest.approx(40 / 60, abs=1e-3)
    assert outcome.baseline_rate_per_day == pytest.approx(23 / (40 / 60) * 24, rel=1e-3)
    assert outcome.baseline_exposure_source == "workload_first_observed"
    assert outcome.as_dict()["baseline_exposure_source"] == "workload_first_observed"


@pytest.mark.parametrize("first", [SCAN - timedelta(seconds=DATA_WINDOW), SCAN - timedelta(days=30)])
def test_an_old_workload_keeps_the_evidence_window(first: datetime) -> None:
    """A workload observed since the window started is exposed for the whole window (never more)."""
    post_start = SCAN + timedelta(hours=1)
    _, outcome = container_reliability(
        [_young_member(first_observed=first)], _gates(0), post_start, post_start + timedelta(hours=1.2), 0
    )
    assert outcome is not None
    assert outcome.baseline_hours == pytest.approx(8.4)
    assert outcome.baseline_rate_per_day == pytest.approx(23 / 8.4 * 24, rel=1e-3)


@pytest.mark.parametrize("first_observed", [None, SCAN + timedelta(minutes=5)])
def test_an_unknown_exposure_falls_back_to_the_evidence_window(first_observed: datetime | None) -> None:
    """Evidence without a usable first observation (stored before the field existed, or after the window)
    keeps the evidence window, and the measurement diagnostics say so."""
    post_start = SCAN + timedelta(hours=1)
    _, outcome = container_reliability(
        [_young_member(first_observed=first_observed)], _gates(0), post_start, post_start + timedelta(hours=1.2), 0
    )
    assert outcome is not None
    assert outcome.baseline_hours == pytest.approx(8.4)
    assert outcome.baseline_exposure_source == "evidence_window"
    _, legacy = container_reliability(
        [_member(oom_events=12)], _gates(0), post_start, post_start + timedelta(days=7), 0
    )
    assert legacy is not None and legacy.baseline_exposure_source == "evidence_window"
    assert legacy.baseline_hours == pytest.approx(WEEK / 3600)


def test_the_live_scenario_verdict_flips_to_realized() -> None:
    """Live e2e: 23 kills in the 40 minutes before the change, none in the 1.2 hours after.

    Against the diluted 8.4-hour baseline the interim check (30 minutes in) was inconclusive; on the
    workload's real exposure it is realized, and so is the whole post-window, with a p-value far below
    the diluted one."""
    post_start = SCAN + timedelta(minutes=10)
    interim_end = post_start + timedelta(minutes=30)
    young = _young_member(first_observed=SCAN - timedelta(minutes=40))
    _, diluted = container_reliability([_young_member(first_observed=None)], _gates(0), post_start, interim_end, 0)
    _, interim = container_reliability([young], _gates(0), post_start, interim_end, 0)
    assert diluted is not None and diluted.outcome is ChangeOutcome.INCONCLUSIVE
    assert interim is not None and interim.outcome is ChangeOutcome.REALIZED

    final_end = post_start + timedelta(hours=1.2)
    _, before_fix = container_reliability([_young_member(first_observed=None)], _gates(0), post_start, final_end, 0)
    _, final = container_reliability([young], _gates(0), post_start, final_end, 0)
    assert final is not None and final.outcome is ChangeOutcome.REALIZED
    assert before_fix is not None and before_fix.p_value is not None and final.p_value is not None
    assert final.p_value < before_fix.p_value / 1000


def test_the_change_level_outcome_keeps_the_exposure_of_young_containers() -> None:
    """Two raised containers of one young workload: the summed outcome uses their real exposure."""
    post_start = SCAN + timedelta(hours=1)
    members = [
        _young_member(first_observed=SCAN - timedelta(minutes=40)),
        _young_member(first_observed=SCAN - timedelta(minutes=40), oom_events=5, container="worker"),
    ]
    _, outcome = increase_reliability(
        members,
        {"api": _gates(0), "worker": _gates(0)},
        post_start,
        post_start + timedelta(hours=1.2),
        {"api": 0, "worker": 0},
    )
    assert outcome is not None and outcome.baseline_events == 28
    assert outcome.baseline_hours == pytest.approx(40 / 60, abs=1e-3)
    assert outcome.baseline_exposure_source == "workload_first_observed"

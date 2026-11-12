"""Reliability outcome of request increases: the OOM-kill rate drop (roadmap H1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from greenkube.models.verification import HealthGateResult, HealthGateStatus
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


def _gates(oom: int | None) -> list[HealthGateResult]:
    gates = [HealthGateResult(gate="restarts", status=HealthGateStatus.PASSED, value=0, threshold=0)]
    if oom is None:
        gates.append(HealthGateResult(gate="oom_kills", status=HealthGateStatus.NOT_AVAILABLE, detail="no source"))
    else:
        status = HealthGateStatus.FAILED if oom > 0 else HealthGateStatus.PASSED
        gates.append(HealthGateResult(gate="oom_kills", status=status, value=oom, threshold=0))
    return gates


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


def test_an_oom_increase_passes_its_gate_when_the_rate_drops() -> None:
    """Regression (H1): one OOM kill after an OOM-driven increase sent it to rollback review, which would
    re-introduce the kills. The rate comparison replaces the "any kill fails" gate."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates, outcome = container_reliability([_member(oom_events=12)], _gates(1), start, start + timedelta(days=7))
    (oom_gate,) = [gate for gate in gates if gate.gate == "oom_kills"]
    assert oom_gate.status is HealthGateStatus.PASSED
    assert oom_gate.value == 1 and oom_gate.threshold == 12
    assert outcome is not None and outcome.outcome is ChangeOutcome.REALIZED
    assert [gate.gate for gate in gates] == ["restarts", "oom_kills"]


def test_an_increase_that_did_not_help_fails_its_gate() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates, outcome = container_reliability([_member(oom_events=2)], _gates(5), start, start + timedelta(days=7))
    (oom_gate,) = [gate for gate in gates if gate.gate == "oom_kills"]
    assert oom_gate.status is HealthGateStatus.FAILED
    assert outcome is not None and outcome.outcome is ChangeOutcome.NOT_REALIZED


def test_decreases_and_increases_without_oom_history_keep_their_gates() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    end = start + timedelta(days=7)
    decrease = _member(current=768 * MIB, proposed=512 * MIB)
    assert not is_reliability_increase(decrease)
    gates = _gates(1)
    assert container_reliability([decrease], gates, start, end) == (gates, None)

    kept, outcome = container_reliability([_member(oom_events=0)], gates, start, end)
    assert kept == gates
    assert outcome is not None and outcome.outcome is ChangeOutcome.INCONCLUSIVE


def test_unknown_post_window_kills_keep_the_gate_unavailable() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates = _gates(None)
    kept, outcome = container_reliability([_member()], gates, start, start + timedelta(days=7))
    assert kept == gates
    assert outcome is not None and outcome.outcome is ChangeOutcome.INCONCLUSIVE


@pytest.mark.asyncio
async def test_the_measurement_service_judges_increase_gates_on_the_rate() -> None:
    """The change-measurement health check (interim and final) applies the rate comparison."""
    from unittest.mock import MagicMock

    from greenkube.verify.service import ChangeMeasurementService

    class _Verifier:
        async def health_gates_for(self, group, start, end, *, strict=False):
            return _gates(1)

    service = ChangeMeasurementService(
        MagicMock(), MagicMock(), MagicMock(), verifier=cast(Any, _Verifier()), config=MagicMock()
    )
    start = datetime.now(timezone.utc) - timedelta(days=7)
    gates = await service._health(cast(Any, [_member(oom_events=12)]), start, start + timedelta(days=7))
    (oom_gate,) = [gate for gate in gates if gate.gate == "oom_kills"]
    assert oom_gate.status is HealthGateStatus.PASSED

    decrease = await service._health(
        cast(Any, [_member(current=768 * MIB, proposed=512 * MIB)]), start, start + timedelta(days=7)
    )
    assert [gate.status for gate in decrease if gate.gate == "oom_kills"] == [HealthGateStatus.FAILED]


def _oom_gate(gates: list[HealthGateResult]) -> HealthGateResult:
    (gate,) = [gate for gate in gates if gate.gate == "oom_kills"]
    return gate


def test_a_decreased_container_keeps_its_strict_gate_in_a_mixed_plan() -> None:
    """Regression (review): one container's OOM-driven memory increase must not hide the OOM kills of
    another container whose memory was lowered in the same change."""
    start = datetime.now(timezone.utc) - timedelta(days=1)
    end = start + timedelta(days=1)
    increase = _member(container="a", oom_events=20)
    decrease = _member(container="b", current=1024 * MIB, proposed=256 * MIB, oom_events=0)
    judged, outcome = increase_reliability([increase, decrease], {"a": _gates(0), "b": _gates(2)}, start, end)
    assert _oom_gate(judged["b"]).status is HealthGateStatus.FAILED
    assert _oom_gate(judged["b"]).value == 2
    assert _oom_gate(judged["a"]).status is HealthGateStatus.PASSED
    # The change-level outcome only counts the increased container: 20 kills a week, none since.
    assert outcome is not None and outcome.baseline_events == 20 and outcome.post_events == 0


def test_a_cpu_only_increase_keeps_the_any_kill_gate() -> None:
    """A CPU increase cannot cure OOM kills: kills after it still fail the gate, whatever the rate."""
    start = datetime.now(timezone.utc) - timedelta(days=7)
    end = start + timedelta(days=7)
    cpu = _cpu_member(oom_events=20)
    assert is_reliability_increase(cpu) and not raises_memory_request(cpu)
    gates, outcome = container_reliability([cpu], _gates(1), start, end)
    assert _oom_gate(gates).status is HealthGateStatus.FAILED
    assert outcome is None
    judged, change_outcome = increase_reliability([cpu], {"api": _gates(1)}, start, end)
    assert _oom_gate(judged["api"]).status is HealthGateStatus.FAILED
    assert change_outcome is None


def test_a_cpu_and_memory_increase_on_one_container_is_judged_on_the_rate() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    end = start + timedelta(days=7)
    members = [_cpu_member(oom_events=12), _member(oom_events=12)]
    gates, outcome = container_reliability(members, _gates(1), start, end)
    assert _oom_gate(gates).status is HealthGateStatus.PASSED
    assert _oom_gate(gates).threshold == 12
    assert outcome is not None and outcome.outcome is ChangeOutcome.REALIZED


def test_a_cpu_increase_with_a_memory_decrease_keeps_the_strict_gate() -> None:
    start = datetime.now(timezone.utc) - timedelta(days=7)
    members = [_cpu_member(oom_events=12), _member(current=768 * MIB, proposed=512 * MIB, oom_events=12)]
    gates, outcome = container_reliability(members, _gates(1), start, start + timedelta(days=7))
    assert _oom_gate(gates).status is HealthGateStatus.FAILED
    assert outcome is None


@pytest.mark.asyncio
async def test_the_measurement_service_keeps_decrease_gates_strict_in_a_mixed_change() -> None:
    from unittest.mock import MagicMock

    from greenkube.verify.service import ChangeMeasurementService

    class _Verifier:
        async def health_gates_for(self, group, start, end, *, strict=False):
            return _gates(2 if group[0].container_name == "b" else 0)

    service = ChangeMeasurementService(
        MagicMock(), MagicMock(), MagicMock(), verifier=cast(Any, _Verifier()), config=MagicMock()
    )
    start = datetime.now(timezone.utc) - timedelta(days=1)
    members = [
        _member(container="a", oom_events=20),
        _member(container="b", current=1024 * MIB, proposed=256 * MIB, oom_events=0),
    ]
    gates, outcome = await service._health_and_reliability(cast(Any, members), start, start + timedelta(days=1))
    failed = [gate for gate in gates if gate.status is HealthGateStatus.FAILED]
    assert [gate.gate for gate in failed] == ["oom_kills"]
    assert failed[0].detail.startswith("[b]")
    assert outcome is not None and outcome.post_events == 0

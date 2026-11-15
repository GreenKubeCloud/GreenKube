"""Health gates judged against the baseline window (MEA-07)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional, cast
from unittest.mock import MagicMock

import pytest

from greenkube.models.verification import HealthGateResult, HealthGateStatus, WorkloadHealthObservation
from greenkube.verify.health_baseline import BASELINE_MISSING, compare_with_baseline, freeze_health
from greenkube.verify.model import BaselineSnapshot, TimeWindow

WEEK_HOURS = 168.0
START = datetime(2026, 6, 1, tzinfo=timezone.utc)


def _gates(restarts: Optional[float], throttling: Optional[float], readiness: Optional[float] = 1.0):
    """Gates as the verifier evaluates them on absolute levels (restart delta 0, throttling 5%, readiness 99%)."""

    def gate(name: str, value: Optional[float], failed: bool, threshold: float) -> HealthGateResult:
        if value is None:
            return HealthGateResult(gate=name, status=HealthGateStatus.NOT_AVAILABLE, detail="not read")
        return HealthGateResult(
            gate=name,
            status=HealthGateStatus.FAILED if failed else HealthGateStatus.PASSED,
            value=value,
            threshold=threshold,
            detail=f"{name} {value}",
        )

    return [
        gate("restarts", restarts, restarts is not None and restarts > 0, 0),
        gate("oom_kills", 0, False, 0),
        gate("readiness", readiness, readiness is not None and readiness < 0.99, 0.99),
        gate("cpu_throttling", throttling, throttling is not None and throttling > 0.05, 0.05),
    ]


def _judge(baseline, post, *, replicas: float = 1.0):
    health = (
        freeze_health({"api": baseline}, replica_hours=replicas * WEEK_HOURS, hours=WEEK_HOURS)
        if baseline is not None
        else {}
    )
    return {
        gate.gate: gate
        for gate in compare_with_baseline(
            post,
            "api",
            health,
            post_hours=WEEK_HOURS,
            post_replica_hours=replicas * WEEK_HOURS,
            max_restart_delta=0,
            max_throttle_ratio=0.05,
            min_readiness=0.99,
        )
    }


def test_unchanged_restarts_and_throttling_pass():
    # Baseline 3 restarts/week and 8% throttling, the same after: nothing got worse.
    judged = _judge(_gates(3, 0.08), _gates(3, 0.08))
    assert all(gate.status is HealthGateStatus.PASSED for gate in judged.values())
    assert judged["restarts"].baseline_value == 3
    assert judged["cpu_throttling"].baseline_value == pytest.approx(0.08)


def test_a_restart_burst_on_a_quiet_workload_fails():
    judged = _judge(_gates(0, 0.0), _gates(6, 0.0))
    assert judged["restarts"].status is HealthGateStatus.FAILED
    assert judged["restarts"].value == 6 and judged["restarts"].baseline_value == 0


def test_one_unrelated_restart_does_not_fail():
    assert _judge(_gates(0, 0.0), _gates(1, 0.0))["restarts"].status is HealthGateStatus.PASSED


def test_the_restart_tolerance_scales_with_replica_hours():
    # 10 replicas: 4 restarts a week before, 7 after is within noise.
    assert _judge(_gates(4, 0.0), _gates(7, 0.0), replicas=10)["restarts"].status is HealthGateStatus.PASSED


def test_throttling_and_readiness_fail_on_a_worsening():
    judged = _judge(_gates(0, 0.08, readiness=0.995), _gates(0, 0.20, readiness=0.95))
    assert judged["cpu_throttling"].status is HealthGateStatus.FAILED
    assert judged["readiness"].status is HealthGateStatus.FAILED


def test_without_a_baseline_the_absolute_rule_is_kept():
    post = _gates(1, 0.08)
    judged = _judge(None, post)
    assert judged["restarts"].status is HealthGateStatus.FAILED
    assert judged["cpu_throttling"].status is HealthGateStatus.FAILED
    assert BASELINE_MISSING in judged["restarts"].detail
    assert judged["restarts"].baseline_value is None


def test_snapshot_digest_of_older_snapshots_still_verifies():
    window = TimeWindow(start=START - timedelta(days=7), end=START)
    old = BaselineSnapshot(snapshot_id="s", change_id="c", window=window).with_digest()
    assert "health" not in old.digest_payload()
    frozen = old.model_copy(update={"health": freeze_health({}, replica_hours=None, hours=WEEK_HOURS)}).with_digest()
    assert frozen.verify_digest() and frozen.content_sha256 != old.content_sha256


class _Verifier:
    """Health check returning the given gates per window (baseline before START, post after)."""

    def __init__(self, before, after) -> None:
        self.before = before
        self.after = after

    async def health_check_for(self, group, start, end, *, strict=False):
        gates = self.before if end <= START else self.after
        return gates, WorkloadHealthObservation(oom_kill_count=0, oom_restart_count=0, readiness_ratio=1.0)


def _service(verifier):
    from greenkube.verify.service import ChangeMeasurementService

    config = MagicMock()
    config.VERIFICATION_MAX_RESTART_DELTA = 0
    config.VERIFICATION_MAX_THROTTLE_RATIO = 0.05
    config.VERIFICATION_MIN_READINESS = 0.99
    return ChangeMeasurementService(MagicMock(), MagicMock(), MagicMock(), verifier=cast(Any, verifier), config=config)


def _member():
    return SimpleNamespace(
        type="RIGHTSIZING_CPU",
        container_name="api",
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=250,
        current_memory_request_bytes=None,
        recommended_memory_request_bytes=None,
        evidence=SimpleNamespace(oom_events=0, observation_window_seconds=7 * 86400.0),
    )


@pytest.mark.asyncio
async def test_the_measurement_service_compares_the_final_gates_with_the_frozen_baseline():
    service = _service(_Verifier(_gates(3, 0.08), _gates(3, 0.08)))
    members = cast(Any, [_member()])
    health = await service._baseline_health(members, TimeWindow(start=START - timedelta(days=7), end=START), [])
    assert health["containers"]["api"]["restarts"] == 3
    gates, _ = await service._health_and_reliability(
        members, START, START + timedelta(days=7), baseline=health, post_replica_hours=None
    )
    assert all(gate.status is not HealthGateStatus.FAILED for gate in gates)

    burst = _service(_Verifier(_gates(0, 0.0), _gates(6, 0.0)))
    quiet = await burst._baseline_health(members, TimeWindow(start=START - timedelta(days=7), end=START), [])
    gates, _ = await burst._health_and_reliability(
        members, START, START + timedelta(days=7), baseline=quiet, post_replica_hours=None
    )
    assert [gate.gate for gate in gates if gate.status is HealthGateStatus.FAILED] == ["restarts"]

    legacy, _ = await service._health_and_reliability(members, START, START + timedelta(days=7), baseline={})
    restarts = next(gate for gate in legacy if gate.gate == "restarts")
    assert restarts.status is HealthGateStatus.FAILED and BASELINE_MISSING in restarts.detail


def _compare(baseline_gates, post_gates, *, baseline_hours: float):
    health = freeze_health({"api": baseline_gates}, replica_hours=baseline_hours, hours=baseline_hours)
    return {
        gate.gate: gate
        for gate in compare_with_baseline(
            post_gates,
            "api",
            health,
            post_hours=WEEK_HOURS,
            post_replica_hours=WEEK_HOURS,
            max_restart_delta=0,
            max_throttle_ratio=0.05,
            min_readiness=0.99,
        )
    }


def test_a_one_hour_baseline_keeps_the_absolute_restart_rule():
    # Review: with 1 baseline hour the post-window share is 168/169 and 100 restarts passed the test.
    judged = _compare(_gates(0, 0.0), _gates(100, 0.0), baseline_hours=1.0)
    assert judged["restarts"].status is HealthGateStatus.FAILED
    assert "baseline_too_short" in (judged["restarts"].detail or "")


def test_a_baseline_a_quarter_of_the_post_window_is_still_compared():
    judged = _compare(_gates(0, 0.0), _gates(1, 0.0), baseline_hours=WEEK_HOURS / 4)
    assert judged["restarts"].status is HealthGateStatus.PASSED
    assert judged["restarts"].baseline_value == 0


def _oom_gates(oom: int, restarts: int):
    return [
        HealthGateResult(
            gate="restarts",
            status=HealthGateStatus.FAILED if restarts > 0 else HealthGateStatus.PASSED,
            value=restarts,
            threshold=0,
        ),
        HealthGateResult(
            gate="oom_kills",
            status=HealthGateStatus.FAILED if oom > 0 else HealthGateStatus.PASSED,
            value=oom,
            threshold=0,
        ),
    ]


class _OomVerifier:
    """Baseline: 10 restarts, all OOM kills. Post: 1 OOM restart and 5 other restarts."""

    async def health_check_for(self, group, start, end, *, strict=False):
        if end <= START:
            observation = WorkloadHealthObservation(oom_kill_count=4, oom_restart_count=10, readiness_ratio=1.0)
            return _oom_gates(4, 10), observation
        observation = WorkloadHealthObservation(oom_kill_count=1, oom_restart_count=1, readiness_ratio=1.0)
        return _oom_gates(1, 6), observation


def _memory_member():
    mib = 1024**2
    return SimpleNamespace(
        type="RIGHTSIZING_MEMORY",
        container_name="api",
        current_cpu_request_millicores=None,
        recommended_cpu_request_millicores=None,
        current_memory_request_bytes=512 * mib,
        recommended_memory_request_bytes=768 * mib,
        evidence=SimpleNamespace(oom_events=10, observation_window_seconds=7 * 86400.0),
    )


@pytest.mark.asyncio
async def test_oom_restarts_are_excused_on_both_sides_of_the_restart_comparison():
    # Review: a memory increase drops OOM restarts from the post count while the baseline count kept
    # them, so 10 OOM restarts before hid a rise of other restarts. Both sides must count the same.
    service = _service(_OomVerifier())
    members = cast(Any, [_memory_member()])
    window = TimeWindow(start=START - timedelta(days=7), end=START)
    health = await service._baseline_health(members, window, [])
    assert health["containers"]["api"]["oom_restarts"] == 10
    gates, _ = await service._health_and_reliability(
        members, START, START + timedelta(days=7), baseline=health, post_replica_hours=None
    )
    restarts = next(gate for gate in gates if gate.gate == "restarts")
    # 5 other restarts after against 0 other restarts before: a significant rise.
    assert restarts.baseline_value == 0 and restarts.value == 5
    assert restarts.status is HealthGateStatus.FAILED

    # A snapshot without baseline OOM restarts cannot be compared on equal terms: absolute rule, labelled.
    del health["containers"]["api"]["oom_restarts"]
    gates, _ = await service._health_and_reliability(
        members, START, START + timedelta(days=7), baseline=health, post_replica_hours=None
    )
    restarts = next(gate for gate in gates if gate.gate == "restarts")
    assert "baseline_oom_restarts_missing" in (restarts.detail or "")

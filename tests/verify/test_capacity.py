"""Measured released capacity from workload specs (WP-08.1 review fixes)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from greenkube.verify.capacity import measure_capacity

T = datetime(2026, 1, 10, tzinfo=timezone.utc)
GIB = 1024**3


def _obs(at, resources, digest=None):
    return SimpleNamespace(observed_at=at, container_resources=resources, spec_digest=digest)


def _req(cpu, memory, kind=None):
    values = {"cpu_request_millicores": cpu, "memory_request_bytes": memory}
    if kind is not None:
        values["kind"] = kind
    return values


def _change(containers=("api",), *, started=None, completed=None, merge=None, digest_before=None):
    completed = completed or T
    return SimpleNamespace(
        baseline_window_end=merge or completed,
        rollout_started_at=started or completed - timedelta(minutes=5),
        rollout_completed_at=completed,
        spec_digest_before=digest_before,
        containers=list(containers),
        details={},
    )


def _cpu_cut(current=1000, proposed=300):
    return SimpleNamespace(
        current_cpu_request_millicores=current,
        recommended_cpu_request_millicores=proposed,
        actual_cpu_request_millicores=None,
        current_memory_request_bytes=None,
        recommended_memory_request_bytes=None,
        actual_memory_request_bytes=None,
    )


def _buckets(replicas, hours=24):
    return [SimpleNamespace(replicas=replicas)] * hours


def test_a_manual_apply_seen_before_the_baseline_end_still_compares_with_the_old_spec():
    # Manual apply: the baseline ends at the rollout completion, after the new revision was first seen.
    change = _change(started=T - timedelta(minutes=5), completed=T)
    observations = [
        _obs(T - timedelta(days=3), {"api": _req(1000, 2 * GIB)}),
        _obs(T - timedelta(minutes=5), {"api": _req(300, GIB)}),
    ]
    result = measure_capacity(
        change, observations, _buckets(2), _buckets(2), [_cpu_cut()], post_end=T + timedelta(days=7)
    )
    assert result["verdict"] == "released"
    assert result["per_replica"]["cpu_cores"] == pytest.approx(0.7)


def test_the_before_spec_is_the_one_with_the_recorded_digest():
    change = _change(started=T - timedelta(hours=2), completed=T, digest_before="old")
    observations = [
        _obs(T - timedelta(days=3), {"api": _req(1000, GIB)}, digest="old"),
        _obs(T - timedelta(hours=3), {"api": _req(300, GIB)}, digest="new"),
    ]
    result = measure_capacity(
        change, observations, _buckets(1), _buckets(1), [_cpu_cut()], post_end=T + timedelta(days=7)
    )
    assert result["requested_per_replica"]["before"]["cpu_cores"] == pytest.approx(1.0)
    assert result["verdict"] == "released"


def test_a_removed_container_is_reported_apart_not_booked_as_a_release():
    change = _change(containers=())
    observations = [
        _obs(T - timedelta(days=1), {"api": _req(500, GIB), "side": _req(500, GIB)}),
        _obs(T, {"api": _req(500, GIB)}),
    ]
    result = measure_capacity(change, observations, _buckets(2), _buckets(2), [], post_end=T + timedelta(days=7))
    assert result["per_replica"]["cpu_cores"] == pytest.approx(0.0)
    assert result["verdict"] == "not_released"
    assert result["containers_removed"] == ["side"]
    assert result["containers_added"] == []


def test_init_containers_are_left_out_and_native_sidecars_kept():
    change = _change(containers=())
    observations = [
        _obs(
            T - timedelta(days=1),
            {"api": _req(1000, GIB), "init": _req(2000, GIB, "init"), "proxy": _req(200, GIB, "sidecar")},
        ),
        _obs(T, {"api": _req(300, GIB), "init": _req(100, GIB, "init"), "proxy": _req(100, GIB, "sidecar")}),
    ]
    result = measure_capacity(change, observations, _buckets(1), _buckets(1), [], post_end=T + timedelta(days=7))
    assert result["requested_per_replica"]["before"]["cpu_cores"] == pytest.approx(1.2)
    assert result["per_replica"]["cpu_cores"] == pytest.approx(0.8)


def test_request_effect_plus_replica_effect_equals_the_net_change():
    change = _change(started=T, completed=T + timedelta(minutes=5), merge=T)
    observations = [
        _obs(T - timedelta(days=3), {"api": _req(1000, GIB)}),
        _obs(T + timedelta(minutes=5), {"api": _req(300, GIB)}),
    ]
    result = measure_capacity(
        change, observations, _buckets(2), _buckets(6), [_cpu_cut()], post_end=T + timedelta(days=7)
    )
    request_effect = result["total"]["cpu_cores"]
    replica_effect = result["replica_change"]["cpu_cores"]
    assert request_effect == pytest.approx(0.7 * 2)
    assert replica_effect == pytest.approx(-4 * 0.3)
    assert result["net"]["cpu_cores"] == pytest.approx(1.0 * 2 - 0.3 * 6)
    assert request_effect + replica_effect == pytest.approx(result["net"]["cpu_cores"])
